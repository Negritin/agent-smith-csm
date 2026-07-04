"""Unit tests for ConversationStore (SPEC C1 Phase 0 §8.1).

Conventions (mirror test_chat_turn_orchestrator.py):
  - NO pytest-asyncio; async is driven with asyncio.run(...).
  - Plain asserts; a fake async Supabase client is injected.
  - NO external service touched (no Supabase/Redis/LLM/HTTP).

The fake mirrors the real call shape: ``db.client.table(name).<op>...execute()``
and ``db.client.rpc(name, params).execute()`` are all AWAITABLE. Every executed
operation is recorded so we can assert ownership filters, dedup, atomic RPC use,
and asyncio.gather parallelism (via the recorded op list).
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

from app.services.turn_ports.conversation_store import (
    ConversationOwnershipUnavailable,
    ConversationStore,
    CrossTenantConversationError,
    _media_kind_to_db_type,
)


# =========================================================================== #
# Fake async Supabase client
# =========================================================================== #
class _Result:
    def __init__(self, data: Any) -> None:
        self.data = data


class _Query:
    """Records a single table operation; ``execute`` is awaitable."""

    def __init__(self, store: "FakeAsyncSupabase", table: str) -> None:
        self._store = store
        self._table = table
        self._op = "select"
        self._payload: Any = None
        self._filters: Dict[str, Any] = {}
        self._select_fields: Optional[str] = None

    # --- builder methods (all chainable, all sync) --------------------- #
    def select(self, fields: str = "*", *_a: Any, **_k: Any) -> "_Query":
        self._op = "select"
        self._select_fields = fields
        return self

    def insert(self, payload: Any, *_a: Any, **_k: Any) -> "_Query":
        self._op = "insert"
        self._payload = payload
        return self

    def update(self, payload: Any, *_a: Any, **_k: Any) -> "_Query":
        self._op = "update"
        self._payload = payload
        return self

    def eq(self, col: str, val: Any) -> "_Query":
        self._filters[col] = val
        return self

    def limit(self, *_a: Any, **_k: Any) -> "_Query":
        return self

    # --- terminal ------------------------------------------------------ #
    async def execute(self) -> _Result:
        return await self._store._run_table_op(self)


class _RpcCall:
    def __init__(self, store: "FakeAsyncSupabase", name: str, params: Dict[str, Any]) -> None:
        self._store = store
        self._name = name
        self._params = params

    async def execute(self) -> _Result:
        self._store.ops.append({"kind": "rpc", "name": self._name, "params": self._params})
        return _Result(None)


class _FakeClient:
    def __init__(self, store: "FakeAsyncSupabase") -> None:
        self._store = store

    def table(self, name: str) -> _Query:
        return _Query(self._store, name)

    def rpc(self, name: str, params: Dict[str, Any]) -> _RpcCall:
        return _RpcCall(self._store, name, params)


class FakeAsyncSupabase:
    """Stand-in for AsyncSupabaseClient.

    ``conversations`` is a list of seeded rows (filtered by eq() on execute).
    ``insert_id`` is the id handed back on conversation insert.
    ``ops`` records every executed operation for assertions.
    """

    def __init__(
        self,
        conversations: Optional[List[Dict[str, Any]]] = None,
        insert_id: str = "conv-new",
        duplicate_key_on_insert: bool = False,
        select_raises: bool = False,
    ) -> None:
        self.conversations = conversations or []
        self.insert_id = insert_id
        self.duplicate_key_on_insert = duplicate_key_on_insert
        self.select_raises = select_raises
        self.ops: List[Dict[str, Any]] = []
        # after a duplicate-key insert, the row "appears" for the retry read
        self._duplicate_pending = duplicate_key_on_insert
        self.client = _FakeClient(self)

    async def _run_table_op(self, q: _Query) -> _Result:
        # record before doing work (ordering matters for gather assertions)
        self.ops.append(
            {
                "kind": "table",
                "table": q._table,
                "op": q._op,
                "filters": dict(q._filters),
                "payload": q._payload,
                "select_fields": q._select_fields,
            }
        )

        if q._table == "conversations" and q._op == "select":
            if self.select_raises:
                raise RuntimeError("simulated supabase failure")
            rows = self._match_conversations(q._filters)
            return _Result(rows)

        if q._table == "conversations" and q._op == "insert":
            if self._duplicate_pending:
                self._duplicate_pending = False
                # make the row visible so the retry read finds it
                self.conversations.append(
                    {
                        "id": self.insert_id,
                        "session_id": q._payload.get("session_id"),
                        "company_id": q._payload.get("company_id"),
                        "unread_count": 0,
                    }
                )
                raise RuntimeError('duplicate key value violates unique constraint "23505"')
            row = dict(q._payload)
            row["id"] = self.insert_id
            self.conversations.append(row)
            return _Result([row])

        if q._table == "messages" and q._op == "insert":
            return _Result([q._payload])

        return _Result([])

    def seed_conversation(self, conversation_id: str, company_id: str) -> None:
        """Seed an owning conversation row so append_message's tenancy guard
        (select id from conversations where id=? and company_id=?) passes."""
        self.conversations.append({"id": conversation_id, "company_id": company_id})

    def _match_conversations(self, filters: Dict[str, Any]) -> List[Dict[str, Any]]:
        out = []
        for row in self.conversations:
            if all(row.get(k) == v for k, v in filters.items()):
                out.append(row)
        return out

    # --- assertion helpers -------------------------------------------- #
    def table_ops(self, table: str, op: str) -> List[Dict[str, Any]]:
        return [
            o
            for o in self.ops
            if o["kind"] == "table" and o["table"] == table and o["op"] == op
        ]

    def rpc_calls(self, name: str) -> List[Dict[str, Any]]:
        return [o for o in self.ops if o["kind"] == "rpc" and o["name"] == name]


# =========================================================================== #
# load_owned
# =========================================================================== #
def test_load_owned_hit_by_tenant() -> None:
    db = FakeAsyncSupabase(
        conversations=[
            {"id": "c1", "session_id": "s1", "company_id": "co1", "status": "open", "unread_count": 3}
        ]
    )
    store = ConversationStore(db)
    row = asyncio.run(store.load_owned(session_id="s1", company_id="co1"))
    assert row is not None
    assert row["id"] == "c1"


def test_load_owned_works_with_raw_client_injection() -> None:
    # REGRESSION (prod 503): every call site injects the RAW async client
    # (chat.py:365/594 -> ConversationStore(db.client); orchestrator default
    # :321 -> ConversationStore(async_supabase_client=db.client)), NOT the
    # AsyncSupabaseClient wrapper. The store must handle a client passed
    # directly (no `.client` attribute) — otherwise load_owned blows up with
    # AttributeError -> ConversationOwnershipUnavailable -> 503 on every turn.
    db = FakeAsyncSupabase(
        conversations=[
            {"id": "c1", "session_id": "s1", "company_id": "co1", "status": "open", "unread_count": 3}
        ]
    )
    # Inject the RAW client exactly like production does.
    raw_client = db.client
    assert not hasattr(raw_client, "client"), "raw client must mirror prod (no .client)"
    store = ConversationStore(raw_client)
    row = asyncio.run(store.load_owned(session_id="s1", company_id="co1"))
    assert row is not None and row["id"] == "c1"


def test_load_owned_cross_tenant_raises() -> None:
    # session exists, but for another company
    db = FakeAsyncSupabase(
        conversations=[{"id": "c1", "session_id": "s1", "company_id": "OTHER"}]
    )
    store = ConversationStore(db)
    raised = False
    try:
        asyncio.run(store.load_owned(session_id="s1", company_id="co1"))
    except CrossTenantConversationError:
        raised = True
    assert raised, "cross-tenant access must raise CrossTenantConversationError"


def test_load_owned_query_failure_raises_unavailable() -> None:
    db = FakeAsyncSupabase(select_raises=True)
    store = ConversationStore(db)
    raised = False
    try:
        asyncio.run(store.load_owned(session_id="s1", company_id="co1"))
    except ConversationOwnershipUnavailable:
        raised = True
    assert raised, "query failure must fail closed with ConversationOwnershipUnavailable"


def test_load_owned_absent_returns_none() -> None:
    db = FakeAsyncSupabase(conversations=[])
    store = ConversationStore(db)
    row = asyncio.run(store.load_owned(session_id="s-none", company_id="co1"))
    assert row is None


# =========================================================================== #
# get_or_create
# =========================================================================== #
def test_get_or_create_creates() -> None:
    db = FakeAsyncSupabase(insert_id="conv-xyz")
    store = ConversationStore(db)
    cid = asyncio.run(
        store.get_or_create(
            session_id="s1",
            company_id="co1",
            user_id="u1",
            agent_id="a1",
            channel="web",
            preview="hello",
        )
    )
    assert cid == "conv-xyz"
    inserts = db.table_ops("conversations", "insert")
    assert len(inserts) == 1
    assert inserts[0]["payload"]["company_id"] == "co1"


def test_get_or_create_race_retry_returns_existing() -> None:
    # insert raises duplicate-key; retry read finds the existing row
    db = FakeAsyncSupabase(insert_id="conv-existing", duplicate_key_on_insert=True)
    store = ConversationStore(db)
    cid = asyncio.run(
        store.get_or_create(
            session_id="s1",
            company_id="co1",
            user_id="u1",
            agent_id="a1",
            channel="web",
            preview="hello",
        )
    )
    assert cid == "conv-existing"
    # one failed insert + at least one retry select
    assert len(db.table_ops("conversations", "insert")) == 1
    assert len(db.table_ops("conversations", "select")) >= 1


# =========================================================================== #
# bump_metadata — D7/G3 atomic, never read-modify-write
# =========================================================================== #
def test_bump_metadata_uses_atomic_rpc_and_no_rmw() -> None:
    db = FakeAsyncSupabase()
    store = ConversationStore(db)
    asyncio.run(
        store.bump_metadata(conversation_id="c1", company_id="co1", preview="hi")
    )
    rpcs = db.rpc_calls("increment_conversation_unread")
    assert len(rpcs) == 1
    params = rpcs[0]["params"]
    assert params["p_conversation_id"] == "c1"
    assert params["p_company_id"] == "co1"
    # ownership + atomicity: no select of unread_count, no update statement
    assert db.table_ops("conversations", "update") == []
    selects = [o for o in db.table_ops("conversations", "select")
               if o["select_fields"] and "unread_count" in o["select_fields"]]
    assert selects == [], "bump_metadata must not read-modify-write unread_count"
    # last_message_at must be timezone-aware ISO (contains a UTC offset)
    assert "+00:00" in params["p_last_message_at"]


# =========================================================================== #
# persist_turn
# =========================================================================== #
def test_persist_turn_persist_user_true_inserts_user_assistant_and_bump() -> None:
    db = FakeAsyncSupabase()
    db.seed_conversation("c1", "co1")
    store = ConversationStore(db)
    asyncio.run(
        store.persist_turn(
            conversation={"id": "c1"},
            company_id="co1",
            session_id="s1",
            user_id="u1",
            agent_id="a1",
            channel="web",
            user_message="oi",
            assistant_message="olá",
            assistant_message_id=None,
            persist_user_message=True,
        )
    )
    msg_inserts = db.table_ops("messages", "insert")
    roles = sorted(o["payload"]["role"] for o in msg_inserts)
    assert roles == ["assistant", "user"]
    assert len(db.rpc_calls("increment_conversation_unread")) == 1


def test_persist_turn_persist_user_false_inserts_only_assistant() -> None:
    db = FakeAsyncSupabase()
    db.seed_conversation("c1", "co1")
    store = ConversationStore(db)
    asyncio.run(
        store.persist_turn(
            conversation={"id": "c1"},
            company_id="co1",
            session_id="s1",
            user_id="u1",
            agent_id="a1",
            channel="web",
            user_message="oi",
            assistant_message="olá",
            assistant_message_id=None,
            persist_user_message=False,
        )
    )
    msg_inserts = db.table_ops("messages", "insert")
    roles = [o["payload"]["role"] for o in msg_inserts]
    assert roles == ["assistant"]
    assert len(db.rpc_calls("increment_conversation_unread")) == 1


def test_persist_turn_assistant_message_id_becomes_id_for_dedup() -> None:
    db = FakeAsyncSupabase()
    db.seed_conversation("c1", "co1")
    store = ConversationStore(db)
    asyncio.run(
        store.persist_turn(
            conversation={"id": "c1"},
            company_id="co1",
            session_id="s1",
            user_id="u1",
            agent_id="a1",
            channel="web",
            user_message=None,
            assistant_message="olá",
            assistant_message_id="front-id-123",
            persist_user_message=False,
        )
    )
    msg_inserts = db.table_ops("messages", "insert")
    assistant = [o for o in msg_inserts if o["payload"]["role"] == "assistant"][0]
    assert assistant["payload"]["id"] == "front-id-123"


def test_persist_turn_runs_inserts_and_bump_in_parallel() -> None:
    """asyncio.gather: the three turn ops are dispatched in one batch.

    With the tenancy guard added to append_message, each message insert is now
    preceded by an ownership select (select id from conversations where
    id=? and company_id=?). No get_or_create runs (the conversation is cached),
    so the only conversations selects are the two ownership guards. We assert the
    two message inserts, the RPC, and exactly two ownership selects.
    """
    db = FakeAsyncSupabase()
    db.seed_conversation("c1", "co1")
    store = ConversationStore(db)
    asyncio.run(
        store.persist_turn(
            conversation={"id": "c1"},
            company_id="co1",
            session_id="s1",
            user_id="u1",
            agent_id="a1",
            channel="web",
            user_message="oi",
            assistant_message="olá",
            assistant_message_id=None,
            persist_user_message=True,
        )
    )
    # 2 message inserts + 1 rpc = 3 ops total. The ownership-guard SELECT is
    # SKIPPED on this path: persist_turn obtains conversation_id from a
    # company_id-scoped load_owned/get_or_create, so ownership is already proven
    # and append_message is called with verify_ownership=False (hot-path: no
    # redundant reads per turn). Cross-tenant protection is covered by the
    # dedicated default-verify tests (test_append_message_cross_tenant_*).
    assert len(db.ops) == 3
    assert len(db.table_ops("messages", "insert")) == 2
    assert len(db.rpc_calls("increment_conversation_unread")) == 1
    # Zero conversations selects: no get_or_create re-load AND no redundant guard.
    guard_selects = db.table_ops("conversations", "select")
    assert len(guard_selects) == 0


def test_persist_turn_last_message_at_timezone_aware_on_create() -> None:
    db = FakeAsyncSupabase(insert_id="conv-new")
    store = ConversationStore(db)
    asyncio.run(
        store.persist_turn(
            conversation=None,  # forces get_or_create
            company_id="co1",
            session_id="s1",
            user_id="u1",
            agent_id="a1",
            channel="web",
            user_message=None,
            assistant_message="olá",
            assistant_message_id=None,
            persist_user_message=False,
        )
    )
    inserts = db.table_ops("conversations", "insert")
    assert len(inserts) == 1
    assert "+00:00" in inserts[0]["payload"]["last_message_at"]


# =========================================================================== #
# D6/G2 — cached conversation reuse (no re-load)
# =========================================================================== #
def test_persist_turn_with_cached_conversation_skips_get_or_create() -> None:
    db = FakeAsyncSupabase()
    db.seed_conversation("cached-c1", "co1")
    store = ConversationStore(db)
    asyncio.run(
        store.persist_turn(
            conversation={"id": "cached-c1"},
            company_id="co1",
            session_id="s1",
            user_id="u1",
            agent_id="a1",
            channel="web",
            user_message=None,
            assistant_message="olá",
            assistant_message_id=None,
            persist_user_message=False,
        )
    )
    # zero re-load via get_or_create AND zero redundant ownership guard: the
    # cached conversation is already company_id-owned, so append_message runs
    # with verify_ownership=False. No conversations select happens at all.
    assert db.table_ops("conversations", "insert") == []
    guard_selects = db.table_ops("conversations", "select")
    assert len(guard_selects) == 0


def test_persist_turn_without_conversation_calls_get_or_create() -> None:
    db = FakeAsyncSupabase(insert_id="conv-created")
    store = ConversationStore(db)
    asyncio.run(
        store.persist_turn(
            conversation=None,
            company_id="co1",
            session_id="s1",
            user_id="u1",
            agent_id="a1",
            channel="web",
            user_message=None,
            assistant_message="olá",
            assistant_message_id=None,
            persist_user_message=False,
        )
    )
    assert len(db.table_ops("conversations", "insert")) == 1


# =========================================================================== #
# persist_user_turn — handoff
# =========================================================================== #
def test_persist_user_turn_inserts_user_and_atomic_bump() -> None:
    db = FakeAsyncSupabase()
    db.seed_conversation("c1", "co1")
    store = ConversationStore(db)
    asyncio.run(
        store.persist_user_turn(
            conversation={"id": "c1"},
            company_id="co1",
            session_id="s1",
            user_id="u1",
            agent_id="a1",
            channel="web",
            user_message="preciso de um humano",
        )
    )
    msg_inserts = db.table_ops("messages", "insert")
    assert len(msg_inserts) == 1
    assert msg_inserts[0]["payload"]["role"] == "user"
    # atomic unread+1 via RPC, never read-modify-write
    assert len(db.rpc_calls("increment_conversation_unread")) == 1
    assert db.table_ops("conversations", "update") == []


def test_persist_user_turn_reuses_cached_conversation() -> None:
    db = FakeAsyncSupabase()
    db.seed_conversation("c1", "co1")
    store = ConversationStore(db)
    asyncio.run(
        store.persist_user_turn(
            conversation={"id": "c1"},
            company_id="co1",
            session_id="s1",
            user_id="u1",
            agent_id="a1",
            channel="web",
            user_message="oi",
        )
    )
    # No get_or_create reload (no conversation insert) AND no redundant ownership
    # guard: the cached conversation is already company_id-owned, so the handoff
    # append runs with verify_ownership=False. Zero conversations selects.
    assert db.table_ops("conversations", "insert") == []
    guard_selects = db.table_ops("conversations", "select")
    assert len(guard_selects) == 0


# =========================================================================== #
# append_message — WhatsApp optional kwargs (D9) without affecting /chat
# =========================================================================== #
def test_append_message_defaults_only_text_type() -> None:
    db = FakeAsyncSupabase()
    db.seed_conversation("c1", "co1")
    store = ConversationStore(db)
    asyncio.run(
        store.append_message(
            conversation_id="c1", company_id="co1", role="user", content="oi"
        )
    )
    payload = db.table_ops("messages", "insert")[0]["payload"]
    assert payload["type"] == "text"
    assert "audio_url" not in payload
    assert "image_url" not in payload
    assert "id" not in payload


def test_append_message_whatsapp_kwargs() -> None:
    db = FakeAsyncSupabase()
    db.seed_conversation("c1", "co1")
    store = ConversationStore(db)
    asyncio.run(
        store.append_message(
            conversation_id="c1",
            company_id="co1",
            role="user",
            content="audio msg",
            client_id="wa-id-1",
            type="voice",
            audio_url="https://a/x.ogg",
            image_url="https://a/y.png",
        )
    )
    payload = db.table_ops("messages", "insert")[0]["payload"]
    assert payload["type"] == "voice"
    assert payload["audio_url"] == "https://a/x.ogg"
    assert payload["image_url"] == "https://a/y.png"
    assert payload["id"] == "wa-id-1"


# =========================================================================== #
# append_message — TENANCY GUARD (conversation_id must belong to company_id)
# =========================================================================== #
def test_append_message_verifies_ownership_before_insert() -> None:
    """The ownership guard selects conversations filtered by id+company_id
    BEFORE inserting the message."""
    db = FakeAsyncSupabase()
    db.seed_conversation("c1", "co1")
    store = ConversationStore(db)
    asyncio.run(
        store.append_message(
            conversation_id="c1", company_id="co1", role="user", content="oi"
        )
    )
    # ownership select happened, scoped by company_id, before the insert
    guard = db.table_ops("conversations", "select")
    assert len(guard) == 1
    assert guard[0]["filters"] == {"id": "c1", "company_id": "co1"}
    # ordering: select (guard) recorded before the messages insert
    op_order = [(o["table"], o["op"]) for o in db.ops if o["kind"] == "table"]
    assert op_order == [("conversations", "select"), ("messages", "insert")]


def test_append_message_cross_tenant_rejected_no_insert() -> None:
    """A conversation_id owned by another tenant must NOT produce a message
    insert; it raises CrossTenantConversationError (shell -> 404)."""
    db = FakeAsyncSupabase()
    db.seed_conversation("c1", "OTHER")  # belongs to a different company
    store = ConversationStore(db)
    raised = False
    try:
        asyncio.run(
            store.append_message(
                conversation_id="c1", company_id="co1", role="user", content="oi"
            )
        )
    except CrossTenantConversationError:
        raised = True
    assert raised, "cross-tenant message insert must raise CrossTenantConversationError"
    assert db.table_ops("messages", "insert") == [], "no orphan message must be written"


def test_append_message_unknown_conversation_rejected() -> None:
    """An unknown conversation_id is indistinguishable from cross-tenant
    (anti-enumeration) and is rejected without an insert."""
    db = FakeAsyncSupabase(conversations=[])
    store = ConversationStore(db)
    raised = False
    try:
        asyncio.run(
            store.append_message(
                conversation_id="ghost", company_id="co1", role="user", content="oi"
            )
        )
    except CrossTenantConversationError:
        raised = True
    assert raised
    assert db.table_ops("messages", "insert") == []


def test_append_message_ownership_check_failure_fails_closed() -> None:
    """A DB error during the ownership check fails closed (no insert) with
    ConversationOwnershipUnavailable (shell -> 503)."""
    db = FakeAsyncSupabase(select_raises=True)
    store = ConversationStore(db)
    raised = False
    try:
        asyncio.run(
            store.append_message(
                conversation_id="c1", company_id="co1", role="user", content="oi"
            )
        )
    except ConversationOwnershipUnavailable:
        raised = True
    assert raised, "ownership-check failure must fail closed"
    assert db.table_ops("messages", "insert") == []


# =========================================================================== #
# D3 Fase 3 — media_kind -> type mapping (single helper; R19)
# =========================================================================== #
def test_media_kind_to_db_type_mapping() -> None:
    # audio is the ONLY kind that becomes "voice"; everything else -> "text".
    assert _media_kind_to_db_type("audio") == "voice"
    assert _media_kind_to_db_type("image") == "text"
    assert _media_kind_to_db_type("text") == "text"
    assert _media_kind_to_db_type(None) == "text"
    # the column never receives the raw semantic kinds (R19).
    for kind in ("audio", "image", "text", None):
        assert _media_kind_to_db_type(kind) in ("voice", "text")


# =========================================================================== #
# D3 Fase 3 — persist_user_turn (HANDOFF) media mapping
# =========================================================================== #
def test_persist_user_turn_audio_maps_to_voice_with_audio_url() -> None:
    db = FakeAsyncSupabase()
    db.seed_conversation("c1", "co1")
    store = ConversationStore(db)
    asyncio.run(
        store.persist_user_turn(
            conversation={"id": "c1"},
            company_id="co1",
            session_id="s1",
            user_id="u1",
            agent_id="a1",
            channel="whatsapp",
            user_message="audio msg",
            media_kind="audio",
            audio_url="https://a/x.ogg",
        )
    )
    payload = db.table_ops("messages", "insert")[0]["payload"]
    assert payload["type"] == "voice"
    assert payload["audio_url"] == "https://a/x.ogg"
    assert payload["type"] != "audio", "column must never store the raw kind"


def test_persist_user_turn_image_maps_to_text_with_image_url() -> None:
    db = FakeAsyncSupabase()
    db.seed_conversation("c1", "co1")
    store = ConversationStore(db)
    asyncio.run(
        store.persist_user_turn(
            conversation={"id": "c1"},
            company_id="co1",
            session_id="s1",
            user_id="u1",
            agent_id="a1",
            channel="whatsapp",
            user_message="foto",
            media_kind="image",
            image_url="https://a/y.png",
        )
    )
    payload = db.table_ops("messages", "insert")[0]["payload"]
    assert payload["type"] == "text"
    assert payload["image_url"] == "https://a/y.png"
    assert payload["type"] != "image", "no 'image' value in the type column"


def test_persist_user_turn_text_kind_stays_text() -> None:
    db = FakeAsyncSupabase()
    db.seed_conversation("c1", "co1")
    store = ConversationStore(db)
    asyncio.run(
        store.persist_user_turn(
            conversation={"id": "c1"},
            company_id="co1",
            session_id="s1",
            user_id="u1",
            agent_id="a1",
            channel="web",
            user_message="texto puro",
            media_kind="text",
        )
    )
    payload = db.table_ops("messages", "insert")[0]["payload"]
    assert payload["type"] == "text"
    assert "audio_url" not in payload
    assert "image_url" not in payload


def test_persist_user_turn_defaults_preserve_legacy_text() -> None:
    # No media kwargs at all -> identical to the pre-D3 handoff write.
    db = FakeAsyncSupabase()
    db.seed_conversation("c1", "co1")
    store = ConversationStore(db)
    asyncio.run(
        store.persist_user_turn(
            conversation={"id": "c1"},
            company_id="co1",
            session_id="s1",
            user_id="u1",
            agent_id="a1",
            channel="web",
            user_message="oi",
        )
    )
    payload = db.table_ops("messages", "insert")[0]["payload"]
    assert payload["type"] == "text"
    assert "audio_url" not in payload
    assert "image_url" not in payload


# =========================================================================== #
# D3 Fase 3 — persist_turn (PROCEED) media parity (OQ12)
# =========================================================================== #
def test_persist_turn_image_persists_image_url_on_user_message() -> None:
    db = FakeAsyncSupabase()
    db.seed_conversation("c1", "co1")
    store = ConversationStore(db)
    asyncio.run(
        store.persist_turn(
            conversation={"id": "c1"},
            company_id="co1",
            session_id="s1",
            user_id="u1",
            agent_id="a1",
            channel="web",
            user_message="o que é isto?",
            assistant_message="é um gato",
            assistant_message_id=None,
            persist_user_message=True,
            media_kind="image",
            image_url="https://a/cat.png",
        )
    )
    user = [o for o in db.table_ops("messages", "insert")
            if o["payload"]["role"] == "user"][0]["payload"]
    assert user["type"] == "text", "image PROCEED keeps text type (no 'image' value)"
    assert user["image_url"] == "https://a/cat.png"


def test_persist_turn_audio_keeps_transcript_without_audio_url() -> None:
    # OQ12: in PROCEED the agent consumed the transcript, so audio persists as
    # the transcribed text (type="text") and NO audio_url is written.
    db = FakeAsyncSupabase()
    db.seed_conversation("c1", "co1")
    store = ConversationStore(db)
    asyncio.run(
        store.persist_turn(
            conversation={"id": "c1"},
            company_id="co1",
            session_id="s1",
            user_id="u1",
            agent_id="a1",
            channel="whatsapp",
            user_message="texto transcrito do audio",
            assistant_message="entendi",
            assistant_message_id=None,
            persist_user_message=True,
            media_kind="audio",
            audio_url="https://a/voice.ogg",
        )
    )
    user = [o for o in db.table_ops("messages", "insert")
            if o["payload"]["role"] == "user"][0]["payload"]
    assert user["type"] == "text"
    assert "audio_url" not in user, "PROCEED audio must not force audio_url (OQ12)"
    assert user["content"] == "texto transcrito do audio"


def test_persist_turn_defaults_no_media_preserve_legacy() -> None:
    # No media kwargs -> the user message is a plain text insert (anti-regression).
    db = FakeAsyncSupabase()
    db.seed_conversation("c1", "co1")
    store = ConversationStore(db)
    asyncio.run(
        store.persist_turn(
            conversation={"id": "c1"},
            company_id="co1",
            session_id="s1",
            user_id="u1",
            agent_id="a1",
            channel="web",
            user_message="oi",
            assistant_message="olá",
            assistant_message_id=None,
            persist_user_message=True,
        )
    )
    user = [o for o in db.table_ops("messages", "insert")
            if o["payload"]["role"] == "user"][0]["payload"]
    assert user["type"] == "text"
    assert "audio_url" not in user
    assert "image_url" not in user


# =========================================================================== #
# D3 Fase 3 — get_or_create extra_fields merge + reserved-key protection
# =========================================================================== #
def test_get_or_create_merges_extra_fields() -> None:
    db = FakeAsyncSupabase(insert_id="conv-extra")
    store = ConversationStore(db)
    cid = asyncio.run(
        store.get_or_create(
            session_id="s1",
            company_id="co1",
            user_id="u1",
            agent_id="a1",
            channel="whatsapp",
            preview="oi",
            extra_fields={
                "user_name": "Alice",
                "user_phone": "+5511999999999",
                "agent_name": "Smith",
                "status_color": "#00ff00",
            },
        )
    )
    assert cid == "conv-extra"
    payload = db.table_ops("conversations", "insert")[0]["payload"]
    # extra fields written...
    assert payload["user_name"] == "Alice"
    assert payload["user_phone"] == "+5511999999999"
    assert payload["agent_name"] == "Smith"
    assert payload["status_color"] == "#00ff00"
    # ...and the mandatory/tenancy keys are intact (not overridden).
    assert payload["company_id"] == "co1"
    assert payload["session_id"] == "s1"
    assert payload["status"] == "open"
    assert payload["unread_count"] == 1


def test_get_or_create_extra_fields_cannot_override_reserved_keys() -> None:
    db = FakeAsyncSupabase()
    store = ConversationStore(db)
    raised = False
    try:
        asyncio.run(
            store.get_or_create(
                session_id="s1",
                company_id="co1",
                user_id="u1",
                agent_id="a1",
                channel="web",
                preview="oi",
                extra_fields={"company_id": "EVIL", "user_name": "Alice"},
            )
        )
    except ValueError:
        raised = True
    assert raised, "extra_fields overriding a tenancy key must raise ValueError"
    # fail fast: no insert attempted with the spoofed company_id.
    assert db.table_ops("conversations", "insert") == []


# =========================================================================== #
# V1 (D5, Fase 4a) — ConversationStore x forma do AsyncSupabaseClient REAL
# =========================================================================== #
class AsyncSupabaseClientShapedFake:
    """Fake com a MESMA forma do ``AsyncSupabaseClient`` real.

    Espelha ``app/core/database.py:329-347``: a classe real expõe ``.client``
    como ``@property`` (read-only) devolvendo o ``AsyncClient`` cru, no qual
    ``.table(...)...execute()`` e ``.rpc(...).execute()`` são AWAITABLE.
    Diferente de :class:`FakeAsyncSupabase` (que seta ``client`` como atributo
    de instância), aqui a property é fiel à classe real — provando V1: o store
    funciona com o client async real injetado, sem proxy sync->async.
    """

    def __init__(self, inner: FakeAsyncSupabase) -> None:
        self._inner = inner

    @property
    def client(self) -> _FakeClient:
        return self._inner.client


def test_v1_store_load_owned_with_real_async_client_shape() -> None:
    inner = FakeAsyncSupabase(
        conversations=[
            {"id": "c1", "session_id": "s1", "company_id": "co1", "status": "open", "unread_count": 0}
        ]
    )
    store = ConversationStore(AsyncSupabaseClientShapedFake(inner))
    row = asyncio.run(store.load_owned(session_id="s1", company_id="co1"))
    assert row is not None and row["id"] == "c1"


def test_v1_store_get_or_create_with_real_async_client_shape() -> None:
    inner = FakeAsyncSupabase(insert_id="conv-real-shape")
    store = ConversationStore(AsyncSupabaseClientShapedFake(inner))
    cid = asyncio.run(
        store.get_or_create(
            session_id="s1",
            company_id="co1",
            user_id="u1",
            agent_id="a1",
            channel="whatsapp",
            preview="oi",
        )
    )
    assert cid == "conv-real-shape"
    assert len(inner.table_ops("conversations", "insert")) == 1


def test_v1_store_append_message_with_real_async_client_shape() -> None:
    inner = FakeAsyncSupabase()
    inner.seed_conversation("c1", "co1")
    store = ConversationStore(AsyncSupabaseClientShapedFake(inner))
    asyncio.run(
        store.append_message(
            conversation_id="c1", company_id="co1", role="user", content="oi"
        )
    )
    assert len(inner.table_ops("messages", "insert")) == 1


def test_v1_store_bump_metadata_rpc_with_real_async_client_shape() -> None:
    inner = FakeAsyncSupabase()
    store = ConversationStore(AsyncSupabaseClientShapedFake(inner))
    asyncio.run(
        store.bump_metadata(conversation_id="c1", company_id="co1", preview="hi")
    )
    assert len(inner.rpc_calls("increment_conversation_unread")) == 1


def test_get_or_create_extra_fields_none_keeps_legacy_payload() -> None:
    db = FakeAsyncSupabase(insert_id="conv-legacy")
    store = ConversationStore(db)
    asyncio.run(
        store.get_or_create(
            session_id="s1",
            company_id="co1",
            user_id="u1",
            agent_id="a1",
            channel="web",
            preview="oi",
        )
    )
    payload = db.table_ops("conversations", "insert")[0]["payload"]
    # exactly the legacy key set — no extra columns leaked in.
    assert set(payload) == {
        "company_id",
        "user_id",
        "session_id",
        "agent_id",
        "channel",
        "status",
        "unread_count",
        "last_message_preview",
        "last_message_at",
    }
