"""
Benchmark Service - Comparação de Estratégias de Chunking + Retrieval (HyDE)

Executa benchmarks comparativos testando:
1. Estratégias de Ingestão (Chunking)
2. Modos de Recuperação (Standard vs HyDE vs Hybrid)

Versão: 2.0 - Com Multi-Agent Isolation
"""

import json
import logging
import os
import random
import re
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from fastembed import SparseTextEmbedding
from langchain_anthropic import ChatAnthropic
from langchain_core.output_parsers import JsonOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_openai import ChatOpenAI, OpenAIEmbeddings

from ..core.config import settings
from ..core.database import get_supabase_client
from ..core.redis import get_redis_client
from .ingestion_service import get_ingestion_service
from .minio_service import get_minio_service
from .qdrant_service import get_qdrant_service
from .rerank_service import get_rerank_service

logger = logging.getLogger(__name__)


class BenchmarkService:
    """
    Serviço de benchmark que compara estratégias de chunking e recuperação.
    Suporta isolamento multi-agent.
    """

    def __init__(self):
        self.minio = get_minio_service()
        self.qdrant = get_qdrant_service()
        self.ingestion = get_ingestion_service()
        self.reranker = get_rerank_service()
        self.supabase = get_supabase_client().client

        # Embeddings (consistente com todo o sistema)
        self.embeddings = OpenAIEmbeddings(
            model="text-embedding-3-small", api_key=settings.OPENAI_API_KEY
        )

        # Modelo para vetores esparsos (BM25)
        self.sparse_model = SparseTextEmbedding(model_name="Qdrant/bm25")

        # LLMs serão criados dinamicamente com billing context
        self.llm = None
        self.hyde_llm = None

        # Redis client para jobs assíncronos
        self.redis = get_redis_client()

    def _get_llms_with_billing(self, company_id: str, agent_id: str):
        """
        Cria LLMs com callback de billing para empresa/agente específicos.
        Garante que o uso de tokens seja debitado corretamente.
        """
        from ..core.callbacks.cost_callback import CostCallbackHandler

        benchmark_callback = CostCallbackHandler(
            service_type="benchmark",
            company_id=company_id,
            agent_id=agent_id
        )

        # LLM para geração de perguntas - modelo mais capaz
        llm = ChatOpenAI(
            model="gpt-4o",  # Subiu de mini pra 4o para perguntas melhores
            temperature=0.3,
            api_key=settings.OPENAI_API_KEY,
            callbacks=[benchmark_callback],
        )

        # LLM para HyDE
        hyde_llm = ChatOpenAI(
            model="gpt-4o-mini",
            temperature=0.7,
            api_key=settings.OPENAI_API_KEY,
            callbacks=[benchmark_callback],
        )

        # 🆕 LLM JUDGE - Claude Sonnet 4.6 para avaliar qualidade dos chunks
        judge_llm = ChatAnthropic(
            model="claude-sonnet-4-6",
            temperature=0.0,
            api_key=os.getenv("ANTHROPIC_API_KEY"),
            callbacks=[benchmark_callback],
        )

        return llm, hyde_llm, judge_llm

    def update_job_status(self, job_id: str, progress: int, status: str, result=None):
        """Salva o progresso do job no Redis com expiração de 1 hora"""
        if not job_id:
            return

        data = {"status": status, "progress": progress, "result": result}
        try:
            existing = self.redis.get(f"bench:{job_id}")
            if existing:
                try:
                    existing_data = json.loads(existing)
                    if isinstance(existing_data, dict):
                        for key in ("company_id", "agent_id", "document_ids"):
                            if key in existing_data:
                                data[key] = existing_data[key]
                except Exception:
                    pass

            self.redis.set(f"bench:{job_id}", json.dumps(data), ex=3600)
            logger.debug(f"[Job {job_id}] Status: {status} ({progress}%)")
        except Exception as e:
            logger.warning(f"[Redis] Failed to update job status: {e}")

    def _report_progress(
        self,
        progress_callback: Optional[Callable],
        step: str,
        current: int,
        total: int,
        detail: str = "",
    ):
        """Helper para reportar progresso do benchmark"""
        pct = int((current / total) * 100) if total > 0 else 0
        logger.info(f"📊 [{pct}%] {step}: {detail}")
        if progress_callback:
            progress_callback(
                {
                    "step": step,
                    "current": current,
                    "total": total,
                    "percentage": pct,
                    "detail": detail,
                }
            )

    def _generate_hyde_doc(self, question: str) -> str:
        """
        Gera um documento hipotético (alucinação controlada) para HyDE.
        """
        prompt = """Você é um especialista técnico. Escreva um trecho de documento técnico curto e denso que conteria a resposta perfeita para a pergunta abaixo.
        Use terminologia técnica correta. Não responda diretamente, simule o documento.

        Pergunta: {question}

        Trecho Hipotético:"""

        try:
            result = self.hyde_llm.invoke(prompt.format(question=question))
            return result.content
        except Exception as e:
            logger.warning(f"[HyDE] Falha na geração: {e}. Usando pergunta original.")
            return question  # Fallback seguro

    def run_company_benchmark(
        self,
        company_id: str,
        agent_id: str,  # OBRIGATÓRIO: Multi-agent isolation
        document_ids: List[str],  # IDs dos documentos selecionados pelo usuário
        threshold: float = 0.62,  # Parametrizado
        total_questions: int = 10,  # Parametrizado
        progress_callback: Optional[Callable] = None,  # Callback para progresso
        job_id: str = None,  # ID do job para progresso async
    ) -> Dict[str, Any]:
        """
        Executa benchmark MATRIZ (Chunking x Retrieval Mode).
        Compara 4 estratégias de chunking em 3 modos (Standard, HyDE, Hybrid).

        Args:
            company_id: ID da empresa
            agent_id: ID do agente (isola documentos por agente)
            document_ids: Lista de IDs de documentos selecionados pelo usuário
            threshold: Score mínimo para considerar match (default 0.62)
            total_questions: Total de perguntas a gerar (default 10)
            progress_callback: Função opcional para reportar progresso
            job_id: ID do job para atualizar progresso no Redis
        """
        import uuid

        start_time = time.time()
        benchmark_id = str(uuid.uuid4())

        logger.info("🏢 ===============================================")
        logger.info(f"🏢 COMPANY BENCHMARK MATRIX - {benchmark_id}")
        logger.info(
            f"🏢 Agent: {agent_id} | Docs: {len(document_ids)} | Questions: {total_questions} | Threshold: {threshold}"
        )
        logger.info(f"🏢 Job ID: {job_id}")
        logger.info("🏢 ===============================================")

        try:
            # Initialize LLMs with billing context for this specific company/agent
            self.llm, self.hyde_llm, self.judge_llm = self._get_llms_with_billing(company_id, agent_id)
            logger.info(f"[Benchmark] 💰 Billing enabled for company={company_id}, agent={agent_id}")
            logger.info("[Benchmark] 🧑‍⚖️ LLM Judge (Claude Sonnet 4.6) initialized")

            # === FASE A: Geração do Dataset ===
            logger.info("\n📚 [PHASE A] Generating Dataset...")
            self.update_job_status(job_id, 5, "generating_dataset")

            # 1. Validar e carregar documentos selecionados pelo usuário
            docs_query = (
                self.supabase.table("documents")
                .select("id, file_name, file_type, status, company_id, agent_id")
                .in_("id", document_ids)
                .execute()
            )

            # Validar que todos existem e pertencem à empresa/agente
            found_docs = {d["id"]: d for d in docs_query.data}
            sample_docs = []
            for doc_id in document_ids:
                doc = found_docs.get(doc_id)
                if not doc:
                    raise ValueError(f"Documento {doc_id} não encontrado.")
                if doc["company_id"] != company_id or doc["agent_id"] != agent_id:
                    raise ValueError(f"Documento {doc_id} não pertence a este agente/empresa.")
                if doc.get("status") != "completed":
                    raise ValueError(f"Documento '{doc['file_name']}' não está completado (status: {doc.get('status')}).")
                if doc.get("file_type") == "csv":
                    raise ValueError(f"Documento CSV '{doc['file_name']}' não é suportado para benchmark.")
                sample_docs.append(doc)

            if not sample_docs:
                raise ValueError("Nenhum documento válido selecionado.")

            # 🔥 PROGRESSO: Documentos carregados
            self._report_progress(
                progress_callback,
                "loading_docs",
                1,
                5,
                f"{len(sample_docs)} docs selecionados",
            )

            # 2. Gerar perguntas (distribuir entre documentos)
            questions_per_doc = max(1, total_questions // len(sample_docs))
            remaining = total_questions - (questions_per_doc * len(sample_docs))

            questions_dataset = []
            for idx, doc in enumerate(sample_docs):
                try:
                    text, _, _ = self._load_document(doc["id"], company_id)
                    # Primeiros docs recebem +1 pergunta se houver resto
                    num_q = questions_per_doc + (1 if idx < remaining else 0)
                    questions = self._generate_questions(text, num_questions=num_q)
                    for q in questions:
                        questions_dataset.append(
                            {"question": q, "source_doc_id": doc["id"]}
                        )
                except Exception as e:
                    logger.warning(f"Skipping questions for {doc['file_name']}: {e}")

            if not questions_dataset:
                raise ValueError("Failed to generate dataset.")

            logger.info(f"✅ Dataset ready: {len(questions_dataset)} questions.")

            # 🔥 PROGRESSO: Perguntas geradas
            self._report_progress(
                progress_callback,
                "generating_questions",
                2,
                5,
                f"{len(questions_dataset)} perguntas geradas",
            )
            self.update_job_status(job_id, 20, "questions_generated")

            # === FASE B: Torneio (Dual-Loop) ===
            logger.info("\n🥊 [PHASE B] Tournament: Chunking x Retrieval Modes")
            strategies_list = ["recursive", "semantic", "page", "agentic"]
            strategies_results = []

            for idx, strategy in enumerate(strategies_list, 1):
                logger.info(
                    f"\n🔬 Round {idx}/4: Testing Strategy '{strategy.upper()}'"
                )

                # 🔥 PROGRESSO: Testando estratégia (25% a 85% distribuído entre 4 estratégias)
                strategy_progress = 25 + int((idx - 1) / len(strategies_list) * 60)
                self._report_progress(
                    progress_callback,
                    "testing_strategy",
                    idx + 2,
                    6,
                    f"Testando {strategy}",
                )
                self.update_job_status(job_id, strategy_progress, f"running_{strategy}")

                # Nome da collection temporária única para esta estratégia
                timestamp = int(time.time() * 1000)
                temp_collection = f"bench_{timestamp}_{strategy}".replace("-", "_")

                try:
                    # 1. Indexação (Apenas uma vez por estratégia!)
                    logger.info(f"   💾 Indexing with '{strategy}' strategy...")
                    self.qdrant.create_collection(
                        company_id, collection_name=temp_collection
                    )

                    has_chunks = False
                    for doc in sample_docs:
                        try:
                            text, raw_data, doc_name = self._load_document(
                                doc["id"], company_id
                            )
                            chunks, metadatas = self.ingestion.apply_chunking(
                                text, raw_data, strategy
                            )
                            if chunks:
                                # Enriquecer metadata
                                for m in metadatas:
                                    m["source_doc_name"] = doc_name

                                # GERAÇÃO DE VETORES (Dense + Sparse)
                                vectors = self.embeddings.embed_documents(chunks)

                                # Gerar vetores esparsos para busca híbrida
                                sparse_vectors = None
                                try:
                                    sparse_vectors = list(
                                        self.sparse_model.embed(chunks)
                                    )
                                except Exception as e:
                                    logger.error(f"Erro ao gerar sparse vectors: {e}")

                                self.qdrant.insert_embeddings(
                                    company_id=company_id,
                                    document_id=doc["id"],
                                    agent_id=agent_id,  # 🔥 Passa agent_id
                                    embeddings=vectors,
                                    chunks=chunks,
                                    metadata=metadatas,
                                    sparse_embeddings=sparse_vectors,
                                    collection_name=temp_collection,
                                )
                                has_chunks = True
                        except Exception as e:
                            logger.error(f"Failed to index {doc['file_name']}: {e}")

                    if not has_chunks:
                        logger.warning(
                            f"   ⚠️ Strategy '{strategy}' failed to produce chunks."
                        )
                        continue

                    # 2. Teste Modo STANDARD
                    logger.info("   🔍 Testing Mode: STANDARD")
                    std_metrics = self._run_retrieval_test(
                        questions_dataset=questions_dataset,
                        collection_name=temp_collection,
                        company_id=company_id,
                        agent_id=agent_id,  # 🔥 Passa agent_id
                        mode="standard",
                        threshold=threshold,
                    )

                    # 3. Teste Modo HyDE
                    logger.info(
                        "   🔍 Testing Mode: HyDE (Generating hallucinations...)"
                    )
                    hyde_metrics = self._run_retrieval_test(
                        questions_dataset=questions_dataset,
                        collection_name=temp_collection,
                        company_id=company_id,
                        agent_id=agent_id,  # 🔥 Passa agent_id
                        mode="hyde",
                        threshold=threshold,
                    )

                    # 4. Teste Modo HYBRID
                    logger.info("   🔍 Testing Mode: HYBRID")
                    hybrid_metrics = self._run_retrieval_test(
                        questions_dataset=questions_dataset,
                        collection_name=temp_collection,
                        company_id=company_id,
                        agent_id=agent_id,  # 🔥 Passa agent_id
                        mode="hybrid",
                        threshold=threshold,
                    )

                    # Registrar Resultados
                    strategies_results.append(
                        {
                            "type": strategy,
                            "count": len(questions_dataset),
                            "modes": {
                                "standard": std_metrics,
                                "hyde": hyde_metrics,
                                "hybrid": hybrid_metrics,
                            },
                        }
                    )

                    logger.info(
                        f"   📊 {strategy.upper()} Result: Std={std_metrics['true_rate']:.1%} | HyDE={hyde_metrics['true_rate']:.1%} | Hybrid={hybrid_metrics['true_rate']:.1%}"
                    )

                finally:
                    # Cleanup da collection
                    self.qdrant.delete_collection(
                        company_id, collection_name=temp_collection
                    )

            # === FASE C: Cálculo do Vencedor ===
            winner_name, final_metadata = self._determine_winner_matrix(
                strategies_results
            )

            # 🔥 PROGRESSO: Completo
            self._report_progress(
                progress_callback, "completed", 6, 6, f"Vencedor: {winner_name}"
            )

            execution_time = time.time() - start_time

            response = {
                "benchmark_id": benchmark_id,
                "agent_id": agent_id,  # 🔥 Retorna agent_id testado
                "threshold": threshold,  # 🔥 Retorna threshold usado
                "winner": winner_name,
                "strategies": strategies_results,
                "metadata": {
                    "total_questions": len(questions_dataset),
                    "sample_size": len(sample_docs),
                    "execution_time_seconds": round(execution_time, 2),
                    **final_metadata,
                },
            }

            # 🔥 Salvar resultado final no Redis
            self.update_job_status(job_id, 100, "completed", result=response)

            return response

        except Exception as e:
            logger.error(f"💥 Benchmark failed: {e}", exc_info=True)
            # 🔥 Salvar erro no Redis
            self.update_job_status(job_id, 0, "failed", result={"error": str(e)})
            raise

    def _run_retrieval_test(
        self,
        questions_dataset: List[Dict],
        collection_name: str,
        company_id: str,
        agent_id: str,  # Obrigatório para multi-agent isolation
        mode: str,  # "standard", "hyde" ou "hybrid"
        threshold: float = 0.62,  # Parametrizado
    ) -> Dict[str, Any]:
        """
        Executa o teste de recuperação para um modo específico.

        Args:
            questions_dataset: Lista de perguntas com source_doc_id
            collection_name: Nome da collection temporária
            company_id: ID da empresa
            agent_id: ID do agente (para filtro na busca)
            mode: Modo de recuperação (standard, hyde, hybrid)
            threshold: Score mínimo para considerar match
        """
        raw_scores = []
        true_positives = 0

        # 🆕 Scores do Judge
        chunk_quality_scores = []

        for q_data in questions_dataset:
            question = q_data["question"]
            expected_doc_id = q_data["source_doc_id"]

            # Prepara a query baseada no modo
            sparse_vector = None
            use_hybrid = False

            if mode == "hyde":
                # HyDE: Gera documento hipotético e usa como query
                query_text = self._generate_hyde_doc(question)
            elif mode == "hybrid":
                # Hybrid: Usa query original + vetor esparso BM25
                query_text = question
                use_hybrid = True
                try:
                    sparse_vector = list(self.sparse_model.embed([question]))[0]
                except Exception as e:
                    logger.warning(f"[Hybrid] Falha ao gerar sparse vector: {e}")
            else:  # mode == "standard"
                query_text = question

            # 1. BUSCA VETORIAL
            q_vector = self.embeddings.embed_query(query_text)

            initial_results = self.qdrant.search_similar(
                company_id=company_id,
                agent_id=agent_id,  # 🔥 FIX: Passa agent_id para a busca
                query_embedding=q_vector,
                sparse_embedding=sparse_vector if use_hybrid else None,
                top_k=20,  # Busca 20 candidatos para o reranker filtrar
                score_threshold=0.0,
                collection_name=collection_name,
            )

            # 2. RERANKING (Precision) - Usa pergunta original, não HyDE
            # Cross-Encoder avalia relevância da resposta para a pergunta real
            results = self.reranker.rerank(
                query=question,  # Sempre usa pergunta original
                docs=initial_results,
                top_k=5,  # Refina para top 5
            )

            # 3. Métricas (sobre resultados reordenados)
            if results:
                # Pega o score do primeiro resultado (melhor match)
                first_result = results[0]
                score = first_result.get("score") or first_result.get("rerank_score", 0)
                if isinstance(score, (int, float)):
                    raw_scores.append(score)
                else:
                    raw_scores.append(0.0)

                # Verifica True Positive (Doc correto com score alto)
                for res in results:
                    res_score = res.get("score") or res.get("rerank_score", 0)
                    if (
                        res.get("document_id") == expected_doc_id
                        and res_score >= threshold
                    ):
                        true_positives += 1
                        break

                # ========================================
                # 🆕 NOVO: Avaliar chunks com Judge
                # ========================================
                chunks_text = [r.get("content", "") for r in results[:5]]
                chunk_score = self._judge_chunks(question, chunks_text)
                if chunk_score > 0:
                    chunk_quality_scores.append(chunk_score)
                else:
                    chunk_quality_scores.append(0.0)
                # ========================================

            else:
                raw_scores.append(0.0)
                chunk_quality_scores.append(0.0)

        count = len(questions_dataset)
        return {
            # Métricas existentes
            "avg_score": round(sum(raw_scores) / count, 4) if count else 0,
            "true_rate": round(true_positives / count, 4) if count else 0,
            "raw_scores": [round(s, 4) for s in raw_scores],
            # 🆕 Métrica do Judge
            "chunk_quality": round(sum(chunk_quality_scores) / len(chunk_quality_scores), 2) if chunk_quality_scores else 0,
            "chunk_quality_scores": chunk_quality_scores,
        }

    def _judge_chunks(self, question: str, chunks: List[str]) -> float:
        """
        Avalia se os chunks recuperados contêm informação suficiente
        para responder a pergunta usando LLM as Judge.

        Args:
            question: Pergunta original
            chunks: Lista de chunks recuperados (top 3-5)

        Returns:
            Score de 1 a 5 (float). Retorna 0 em caso de erro.
        """
        if not chunks:
            return 0.0

        chunks_text = "\n---\n".join(chunks[:5])

        judge_prompt = f"""Você é um avaliador de sistemas RAG. Avalie se os chunks recuperados contêm informação suficiente para responder a pergunta.

PERGUNTA:
{question}

CHUNKS RECUPERADOS:
{chunks_text}

Avalie de 1 a 5:
- 5: Chunks contêm toda informação necessária para uma resposta completa
- 4: Chunks contêm a maior parte da informação, resposta seria boa
- 3: Chunks contêm informação parcial, resposta seria incompleta
- 2: Chunks contêm pouca informação relevante
- 1: Chunks não contêm informação para responder a pergunta

Responda APENAS com o número (1, 2, 3, 4 ou 5), nada mais."""

        try:
            result = self.judge_llm.invoke(judge_prompt)
            score_text = result.content.strip()

            # Extrair número da resposta
            match = re.search(r'[1-5]', score_text)
            if match:
                score = float(match.group())
                logger.debug(f"[Judge] Chunk quality score: {score}")
                return score

            logger.warning(f"[Judge] Resposta inválida: {score_text}")
            return 0.0

        except Exception as e:
            logger.error(f"[Judge] Erro ao avaliar chunks: {e}")
            return 0.0

    def _determine_winner_matrix(
        self, strategies_results: List[Dict]
    ) -> Tuple[str, Dict]:
        """
        Determina vencedor considerando Retrieval + Chunk Quality.

        Critério composto:
        - 60% True Rate (retrieval accuracy)
        - 40% Chunk Quality (avaliação do Judge)
        """
        best_combo = None
        best_score = -1.0
        best_details = {}

        for strat in strategies_results:
            s_type = strat["type"]
            for mode, metrics in strat["modes"].items():
                tr = metrics["true_rate"]
                cq = metrics.get("chunk_quality", 0) / 5  # Normaliza pra 0-1

                # Score composto
                composite = (0.60 * tr) + (0.40 * cq)

                if composite > best_score:
                    best_score = composite
                    best_combo = f"{s_type}_{mode}"
                    best_details = {
                        "true_rate": tr,
                        "chunk_quality": metrics.get("chunk_quality", 0),
                        "composite_score": round(composite, 4)
                    }

        return best_combo or "none", {"winner_true_rate": best_details.get("true_rate", 0), **best_details}

    # === MÉTODOS AUXILIARES ===

    def _load_document(
        self, document_id: str, company_id: str
    ) -> Tuple[str, Dict, str]:
        """Carrega documento do MinIO."""
        # Tentar caminho novo
        raw_path = f"{company_id}/raw/{document_id}.json"
        try:
            file_data = self.minio.download_file(raw_path)
            raw_data = json.load(file_data)
        except Exception:
            # Fallback para caminho antigo
            raw_path = f"companies/{company_id}/raw/{document_id}.json"
            file_data = self.minio.download_file(raw_path)
            raw_data = json.load(file_data)

        # Extrair texto
        text_content = raw_data.get("text_content", "")
        if not text_content:
            # Fallback: concatenar páginas
            pages = raw_data.get("pages", [])
            text_content = "\n".join(
                [p.get("content", "") or p.get("text", "") for p in pages]
            )

        if not text_content:
            raise ValueError("Documento vazio ou sem conteúdo de texto")

        doc_name = raw_data.get("metadata", {}).get("filename", "Unknown")
        return text_content, raw_data, doc_name

    def _generate_questions(self, text: str, num_questions: int = 5) -> List[str]:
        """Gera perguntas técnicas baseadas no texto."""
        text_excerpt = text
        if len(text) > 6000:
            mid = len(text) // 2
            text_excerpt = text[:3000] + "\n...\n" + text[mid : mid + 3000]

        prompt = ChatPromptTemplate.from_messages(
            [
                (
                    "system",
                    """Analise o texto e gere {num_questions} perguntas desafiadoras e técnicas.
            As perguntas devem testar compreensão profunda do conteúdo.
            Retorne JSON: {{ "questions": ["p1", "p2", ...] }}""",
                ),
                ("user", "TEXTO:\n{text}"),
            ]
        )

        chain = prompt | self.llm | JsonOutputParser()
        try:
            result = chain.invoke(
                {"text": text_excerpt, "num_questions": num_questions}
            )
            return result.get("questions", [])[:num_questions]
        except Exception as e:
            logger.warning(f"Falha ao gerar perguntas: {e}")
            return [f"Pergunta genérica {i + 1}?" for i in range(num_questions)]


# Singleton
_benchmark_service = None


def get_benchmark_service() -> BenchmarkService:
    global _benchmark_service
    if _benchmark_service is None:
        _benchmark_service = BenchmarkService()
    return _benchmark_service
