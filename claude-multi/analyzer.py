"""
Analyzer — envia os chunks ao Claude Opus 4.6 e agrega a documentação.

Suporta:
  - Programas pequenos: 1 requisição
  - Programas grandes: múltiplos chunks com documentação parcial concatenada
  - Extended thinking para análises mais profundas (opcional)
  - Retry com backoff exponencial
"""
from __future__ import annotations
import os
import time
import json
import logging
from dataclasses import dataclass
from .prompt_builder import PromptChunk

logger = logging.getLogger(__name__)

# Model string oficial do Claude Opus 4.6
OPUS_MODEL = "claude-opus-4-6"

# Ativa janela de 1M tokens (beta)
CONTEXT_1M_BETA_HEADER = "context-1m-2025-08-07"


@dataclass
class AnalysisResult:
    program_id: str
    total_chunks: int
    documentation: str          # Markdown final agregado
    token_usage: dict           # input/output tokens por chunk
    elapsed_seconds: float


class OpusAnalyzer:

    def __init__(
        self,
        api_key: str | None = None,
        extended_thinking: bool = True,
        thinking_budget: int = 10_000,
        max_retries: int = 3,
    ):
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        if not self.api_key:
            raise ValueError(
                "ANTHROPIC_API_KEY não definida. "
                "Defina a variável de ambiente ou passe api_key= ao instanciar."
            )
        self.extended_thinking = extended_thinking
        self.thinking_budget = thinking_budget
        self.max_retries = max_retries

    # ──────────────────────────────────────────────────────────────────────────
    #  API pública
    # ──────────────────────────────────────────────────────────────────────────

    def analyze(self, chunks: list[PromptChunk], program_id: str) -> AnalysisResult:
        """
        Envia todos os chunks ao Opus 4.6 e retorna a documentação agregada.
        """
        start = time.time()
        partial_docs: list[str] = []
        token_usage: dict = {"chunks": []}

        for chunk in chunks:
            logger.info(
                f"[{program_id}] Enviando chunk {chunk.chunk_index}/{chunk.total_chunks} "
                f"(~{chunk.estimated_tokens:,} tokens)"
            )

            response_text, usage = self._call_api(chunk)
            partial_docs.append(
                f"<!-- chunk {chunk.chunk_index}/{chunk.total_chunks} -->\n{response_text}"
            )
            token_usage["chunks"].append({
                "chunk": chunk.chunk_index,
                "input_tokens": usage.get("input_tokens", 0),
                "output_tokens": usage.get("output_tokens", 0),
            })

        # Agrega chunks em documento final
        documentation = self._aggregate(partial_docs, program_id, chunks[0].total_chunks)

        elapsed = time.time() - start
        total_input  = sum(c["input_tokens"]  for c in token_usage["chunks"])
        total_output = sum(c["output_tokens"] for c in token_usage["chunks"])
        token_usage["total"] = {
            "input_tokens": total_input,
            "output_tokens": total_output
        }

        logger.info(
            f"[{program_id}] Concluído em {elapsed:.1f}s | "
            f"Tokens: {total_input:,} entrada / {total_output:,} saída"
        )

        return AnalysisResult(
            program_id=program_id,
            total_chunks=len(chunks),
            documentation=documentation,
            token_usage=token_usage,
            elapsed_seconds=elapsed,
        )

    # ──────────────────────────────────────────────────────────────────────────
    #  Chamada à API com retry
    # ──────────────────────────────────────────────────────────────────────────

    def _call_api(self, chunk: PromptChunk) -> tuple[str, dict]:
        import urllib.request

        body = self._build_request_body(chunk)
        headers = {
            "Content-Type": "application/json",
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            # Habilita janela de 1M tokens (beta)
            "anthropic-beta": CONTEXT_1M_BETA_HEADER,
        }

        for attempt in range(1, self.max_retries + 1):
            try:
                req = urllib.request.Request(
                    "https://api.anthropic.com/v1/messages",
                    data=json.dumps(body).encode("utf-8"),
                    headers=headers,
                    method="POST"
                )
                with urllib.request.urlopen(req, timeout=600) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    return self._extract_text(data), data.get("usage", {})

            except Exception as exc:
                wait = 2 ** attempt
                logger.warning(
                    f"Tentativa {attempt}/{self.max_retries} falhou: {exc}. "
                    f"Aguardando {wait}s..."
                )
                if attempt == self.max_retries:
                    raise
                time.sleep(wait)

        raise RuntimeError("Todas as tentativas falharam.")

    def _build_request_body(self, chunk: PromptChunk) -> dict:
        messages = [{"role": "user", "content": chunk.user_message}]

        body: dict = {
            "model": OPUS_MODEL,
            "max_tokens": 16_000,
            "system": chunk.system,
            "messages": messages,
        }

        # Extended thinking — melhora significativamente a qualidade
        # da análise de lógica de negócio complexa
        if self.extended_thinking:
            body["thinking"] = {
                "type": "enabled",
                "budget_tokens": self.thinking_budget,
            }
            # Com thinking ativo, temperatura deve ser 1 (requisito da API)
            body["temperature"] = 1

        return body

    @staticmethod
    def _extract_text(data: dict) -> str:
        """Extrai texto da resposta, ignorando blocos de thinking."""
        texts = []
        for block in data.get("content", []):
            if block.get("type") == "text":
                texts.append(block["text"])
        return "\n".join(texts)

    # ──────────────────────────────────────────────────────────────────────────
    #  Agregação de chunks
    # ──────────────────────────────────────────────────────────────────────────

    def _aggregate(
        self,
        partial_docs: list[str],
        program_id: str,
        total_chunks: int
    ) -> str:
        if total_chunks == 1:
            # Remove tag de chunk do documento único
            return partial_docs[0].replace(
                "<!-- chunk 1/1 -->\n", ""
            )

        # Múltiplos chunks: monta documento com separadores claros
        header = (
            f"# Documentação de Regras de Negócio\n"
            f"## Programa: {program_id}\n\n"
            f"> Documento gerado em {total_chunks} partes pelo Claude Opus 4.6.\n\n"
        )
        return header + "\n\n---\n\n".join(
            doc.split("-->\n", 1)[-1].strip()
            for doc in partial_docs
        )
