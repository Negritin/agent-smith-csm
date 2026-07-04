
import logging
from typing import List, Optional, Tuple

from presidio_analyzer import (
    AnalyzerEngine,
    Pattern,
    PatternRecognizer,
)
from presidio_analyzer.nlp_engine import NlpEngineProvider
from presidio_anonymizer import AnonymizerEngine
from presidio_anonymizer.entities import OperatorConfig

logger = logging.getLogger(__name__)


# =============================================================================
# P1 — ALLOWLIST / BLOCKLIST DE PII (corrige mascaramento de topônimos/nomes)
# =============================================================================
# Allowlist de PII REAL mascarado por padrão. Inclui SÓ entidades de alta
# precisão (recognizers BR pattern-based + email/cartão/etc.).
#
# CRÍTICO: PERSON está FORA do default de propósito. Com `pt_core_news_md`, o
# spaCy rotula o acrônimo "CPF" e marcas/nomes capitalizados (ex.: "Amazon")
# como PERSON (score ~0.85) → vira "****" e corrompe perguntas legítimas. Quem
# quiser mascarar nomes deve opt-in via `security_settings.pii_entities`.
#
# US_SSN / US_BANK_NUMBER / US_PASSPORT / MEDICAL_LICENSE são no-ops em pt
# (sem recognizer no idioma) — mantidos por robustez multi-idioma, mas NÃO
# disparam em texto pt.
DEFAULT_PII_ENTITIES: List[str] = [
    "BR_CPF",
    "BR_CNPJ",
    "BR_PHONE",
    "EMAIL_ADDRESS",
    "PHONE_NUMBER",
    "CREDIT_CARD",
    "IBAN_CODE",
    "IP_ADDRESS",
    "CRYPTO",
    "US_SSN",
    "US_BANK_NUMBER",
    "US_PASSPORT",
    "MEDICAL_LICENSE",
]

# Blocklist defensiva: NUNCA mascarar topônimos, nacionalidades, datas,
# organizações e URLs — mesmo que venham na config por-empresa. É o que evita
# que "França", "Brasil", "2024" etc. virem "****".
NEVER_MASK_ENTITIES: frozenset = frozenset(
    {
        "LOCATION",
        "GPE",
        "NRP",
        "DATE_TIME",
        "DATE",
        "TIME",
        "ORGANIZATION",
        "ORG",
        "URL",
    }
)

# Threshold mínimo de confiança para considerar um hit como PII.
DEFAULT_PII_SCORE_THRESHOLD: float = 0.5


# =============================================================================
# CUSTOM BRAZILIAN RECOGNIZERS
# =============================================================================

class BrazilianCPFRecognizer(PatternRecognizer):
    """Recognizer for Brazilian CPF (Cadastro de Pessoa Física)."""

    PATTERNS = [
        Pattern(
            "CPF (XXX.XXX.XXX-XX)",
            r"\b\d{3}\.\d{3}\.\d{3}-\d{2}\b",
            0.85
        ),
        Pattern(
            "CPF (XXXXXXXXXXX)",
            r"\b\d{11}\b",
            0.4  # Lower score, needs validation
        ),
    ]

    CONTEXT = ["cpf", "cadastro", "pessoa física", "documento"]

    def __init__(self):
        super().__init__(
            supported_entity="BR_CPF",
            patterns=self.PATTERNS,
            context=self.CONTEXT,
            supported_language="pt",
        )


class BrazilianCNPJRecognizer(PatternRecognizer):
    """Recognizer for Brazilian CNPJ (Cadastro Nacional de Pessoa Jurídica)."""

    PATTERNS = [
        Pattern(
            "CNPJ (XX.XXX.XXX/XXXX-XX)",
            r"\b\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2}\b",
            0.85
        ),
    ]

    CONTEXT = ["cnpj", "empresa", "pessoa jurídica", "razão social"]

    def __init__(self):
        super().__init__(
            supported_entity="BR_CNPJ",
            patterns=self.PATTERNS,
            context=self.CONTEXT,
            supported_language="pt",
        )


class BrazilianPhoneRecognizer(PatternRecognizer):
    """Recognizer for Brazilian phone numbers."""

    PATTERNS = [
        Pattern(
            "BR Phone (+55 XX XXXXX-XXXX)",
            r"\+55\s?\d{2}\s?\d{4,5}[-\s]?\d{4}\b",
            0.85
        ),
        Pattern(
            "BR Phone (XX XXXXX-XXXX)",
            r"\b\d{2}\s?\d{4,5}[-\s]?\d{4}\b",
            0.6
        ),
        Pattern(
            "BR Phone ((XX) XXXXX-XXXX)",
            r"\(\d{2}\)\s?\d{4,5}[-\s]?\d{4}\b",
            0.85
        ),
    ]

    CONTEXT = ["telefone", "celular", "whatsapp", "ligar", "contato", "fone"]

    def __init__(self):
        super().__init__(
            supported_entity="BR_PHONE",
            patterns=self.PATTERNS,
            context=self.CONTEXT,
            supported_language="pt",
        )


class BrazilianEmailRecognizer(PatternRecognizer):
    """Email recognizer with Portuguese context."""

    PATTERNS = [
        Pattern(
            "Email",
            r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b",
            0.9
        ),
    ]

    CONTEXT = ["email", "e-mail", "correio", "enviar"]

    def __init__(self):
        super().__init__(
            supported_entity="EMAIL_ADDRESS",
            patterns=self.PATTERNS,
            context=self.CONTEXT,
            supported_language="pt",
        )


# =============================================================================
# PRESIDIO SERVICE
# =============================================================================

class PresidioService:
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(PresidioService, cls).__new__(cls)
            cls._instance._initialize()
        return cls._instance

    def _initialize(self):
        try:
            # Try to load Portuguese model, fallback to multilingual approach
            try:
                configuration = {
                    "nlp_engine_name": "spacy",
                    "models": [
                        {"lang_code": "pt", "model_name": "pt_core_news_md"},
                        {"lang_code": "en", "model_name": "en_core_web_lg"},
                    ],
                }
                provider = NlpEngineProvider(nlp_configuration=configuration)
                nlp_engine = provider.create_engine()

                self.analyzer = AnalyzerEngine(
                    nlp_engine=nlp_engine,
                    supported_languages=["pt", "en"]
                )
                logger.info("Presidio Service initialized with Portuguese (pt_core_news_md) + English")

            except Exception as model_error:
                logger.warning(f"Could not load pt_core_news_md: {model_error}. Using default engine.")
                self.analyzer = AnalyzerEngine()

            # Register Brazilian recognizers
            self.analyzer.registry.add_recognizer(BrazilianCPFRecognizer())
            self.analyzer.registry.add_recognizer(BrazilianCNPJRecognizer())
            self.analyzer.registry.add_recognizer(BrazilianPhoneRecognizer())
            self.analyzer.registry.add_recognizer(BrazilianEmailRecognizer())

            self.anonymizer = AnonymizerEngine()
            logger.info("Presidio Service initialized with Brazilian recognizers (CPF, CNPJ, Phone, Email)")
            self.initialized = True

        except Exception as e:
            logger.error(f"Failed to initialize Presidio: {e}")
            self.initialized = False

    def _resolve_allowed_entities(self, entities: Optional[List[str]]) -> List[str]:
        """Resolve a allowlist efetiva de entidades a mascarar.

        - `entities=None` → default seguro (`DEFAULT_PII_ENTITIES`, SEM PERSON).
        - Remove qualquer item da blocklist `NEVER_MASK_ENTITIES` (topônimos,
          datas, organizações…) mesmo que venha na config por-empresa.
        - Intersecta com as entidades de fato suportadas pelo analyzer em `pt`,
          para nunca pedir uma entidade sem recognizer (evita o `ValueError` do
          `recognizer_registry`).
        """
        requested = list(entities) if entities else list(DEFAULT_PII_ENTITIES)

        # Remove blocklist (defesa contra config malformada/maliciosa).
        filtered = [e for e in requested if e and e not in NEVER_MASK_ENTITIES]

        # Intersecta com o que o analyzer suporta em pt (proativo, não via except).
        try:
            supported = set(self.analyzer.get_supported_entities(language="pt"))
        except Exception:  # noqa: BLE001 — fail-open: sem suporte conhecido → vazio
            supported = set()

        allowed = [e for e in filtered if e in supported]
        # Preserva ordem e remove duplicatas.
        seen: set = set()
        deduped: List[str] = []
        for e in allowed:
            if e not in seen:
                seen.add(e)
                deduped.append(e)
        return deduped

    def analyze_and_anonymize(
        self,
        text: str,
        action: str = "mask",
        entities: Optional[List[str]] = None,
        score_threshold: Optional[float] = None,
    ) -> Tuple[bool, str]:
        """
        Analisa texto buscando PII e aplica ação (mask ou block).
        Retorna (found_pii: bool, processed_text: str).

        RETROCOMPATÍVEL: `entities`/`score_threshold` são opcionais.
          - `entities=None` → allowlist default segura (PII real, SEM topônimos
            e SEM PERSON — a fonte do bug do CPF→`****`).
          - Blocklist `NEVER_MASK_ENTITIES` removida sempre.
          - Interseção com `get_supported_entities('pt')`; se vazia →
            passthrough explícito `(False, text)`.
          - Refiltro dos resultados (blocklist + threshold) como defesa em
            profundidade.
          - Qualquer exceção → fail-open `(False, text)` (nunca esvazia/trava).
        """
        if not self.initialized or not text:
            return False, text

        threshold = (
            score_threshold
            if score_threshold is not None
            else DEFAULT_PII_SCORE_THRESHOLD
        )

        try:
            # 1. Resolve allowlist efetiva (default seguro + blocklist + supported).
            allowed_entities = self._resolve_allowed_entities(entities)

            # Interseção vazia → não há nada seguro a analisar. Passthrough
            # explícito (não depende do except do recognizer_registry).
            if not allowed_entities:
                return False, text

            # 2. Analyze restringindo às entidades permitidas + threshold.
            results = self.analyzer.analyze(
                text=text,
                language="pt",
                entities=allowed_entities,
                score_threshold=threshold,
            )

            # 3. Refiltro defensivo: remove qualquer entidade da blocklist e
            #    abaixo do threshold (defesa em profundidade — recognizers
            #    custom podem ignorar o score_threshold do analyze).
            results = [
                r
                for r in results
                if r.entity_type not in NEVER_MASK_ENTITIES
                and r.score >= threshold
            ]

            if not results:
                return False, text

            # Log what was found for debugging
            entities_found = [f"{r.entity_type}:{r.score:.2f}" for r in results]
            logger.info(f"[Presidio] PII detected: {entities_found}")

            # 4. Se ação for BLOCK, retorna flag true e texto original
            if action == "block":
                return True, text

            # 5. Se ação for MASK, anonimiza
            if action == "mask":
                anonymized_result = self.anonymizer.anonymize(
                    text=text,
                    analyzer_results=results,
                    operators={
                        "DEFAULT": OperatorConfig("replace", {"new_value": "****"}),
                        "BR_CPF": OperatorConfig("replace", {"new_value": "[CPF OCULTO]"}),
                        "BR_CNPJ": OperatorConfig("replace", {"new_value": "[CNPJ OCULTO]"}),
                        "BR_PHONE": OperatorConfig("replace", {"new_value": "[TELEFONE OCULTO]"}),
                        "PHONE_NUMBER": OperatorConfig("replace", {"new_value": "[TELEFONE OCULTO]"}),
                        "EMAIL_ADDRESS": OperatorConfig("replace", {"new_value": "[EMAIL OCULTO]"}),
                    }
                )
                return True, anonymized_result.text

            return False, text

        except Exception as e:
            logger.error(f"Error in PII analysis: {e}")
            return False, text


def get_presidio_service():
    return PresidioService()
