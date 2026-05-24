"""
Ponto de entrada principal do COBOL Analyzer.

Uso:
  python -m cobol_analyzer analyze <arquivo.cbl> [opções]
  python -m cobol_analyzer analyze <arquivo.cbl> --output doc.md
  python -m cobol_analyzer analyze <arquivo.cbl> --no-thinking --copybook-dir ./copy
"""
from __future__ import annotations
import argparse
import json
import logging
import sys
import time
from pathlib import Path

from extractor.preprocessor import Preprocessor
from extractor.parser import CobolParser
from extractor.enricher import Enricher
from skill.prompt_builder import PromptBuilder
from skill.analyzer import OpusAnalyzer

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)
logger = logging.getLogger(__name__)


def run_pipeline(
    source_path: str,
    output_path: str | None = None,
    copybook_dirs: list[str] | None = None,
    extended_thinking: bool = True,
    thinking_budget: int = 10_000,
    dry_run: bool = False,
    save_extracted: bool = False,
) -> str:
    """
    Pipeline completo: COBOL → extração → Opus 4.6 → documentação Markdown.

    Parâmetros:
      source_path       : caminho do arquivo .cbl / .cob
      output_path       : onde salvar o .md gerado (None = stdout)
      copybook_dirs     : pastas de COPYbooks
      extended_thinking : usa extended thinking do Opus (recomendado)
      thinking_budget   : tokens de raciocínio interno (10k–50k)
      dry_run           : extrai e imprime o payload sem chamar a API
      save_extracted    : salva o JSON extraído ao lado do .md
    """
    path = Path(source_path)
    if not path.exists():
        raise FileNotFoundError(f"Arquivo não encontrado: {source_path}")

    logger.info(f"━━━ Iniciando análise de: {path.name} ━━━")

    # ── 1. Pré-processamento ──────────────────────────────────────────────────
    logger.info("① Pré-processando colunas e COPYbooks...")
    preprocessor = Preprocessor(copybook_dirs=copybook_dirs or [])
    lines, copybooks = preprocessor.process(path)
    logger.info(f"   {len(lines)} linhas processadas | COPYbooks: {copybooks or 'nenhum'}")

    # ── 2. Parse ──────────────────────────────────────────────────────────────
    logger.info("② Extraindo estrutura do programa...")
    parser = CobolParser()
    program = parser.parse(lines, source_file=str(path), copybooks=copybooks)
    logger.info(
        f"   Parágrafos: {len(program.paragraphs)} | "
        f"Variáveis: {len(program.working_storage)} | "
        f"Linkage: {len(program.linkage_section)} | "
        f"Arquivos: {len(program.file_layouts)}"
    )

    # ── 3. Enriquecimento ─────────────────────────────────────────────────────
    logger.info("③ Construindo grafo de chamadas e mapa de variáveis...")
    enricher = Enricher()
    program = enricher.enrich(program)
    dead_count = sum(1 for p in program.paragraphs if p.is_unreachable)
    logger.info(f"   Parágrafos inalcançáveis (código morto): {dead_count}")

    # ── Salva JSON extraído (opcional) ───────────────────────────────────────
    if save_extracted:
        extracted_path = path.with_suffix('.extracted.json')
        _save_extracted_json(program, extracted_path)
        logger.info(f"   JSON extraído salvo em: {extracted_path}")

    # ── 4. Construção do prompt ───────────────────────────────────────────────
    logger.info("④ Montando payload para o Opus 4.6...")
    builder = PromptBuilder()
    chunks = builder.build(program)
    total_tokens = sum(c.estimated_tokens for c in chunks)
    logger.info(
        f"   {len(chunks)} chunk(s) | ~{total_tokens:,} tokens estimados"
    )

    # ── Dry run ───────────────────────────────────────────────────────────────
    if dry_run:
        logger.info("🔍 DRY RUN — payload gerado (sem chamada à API):")
        for chunk in chunks:
            print(f"\n{'═'*60}")
            print(f"CHUNK {chunk.chunk_index}/{chunk.total_chunks} "
                  f"(~{chunk.estimated_tokens:,} tokens)")
            print('═'*60)
            print(chunk.user_message[:3000])
            if len(chunk.user_message) > 3000:
                print(f"\n... [{len(chunk.user_message)-3000} chars omitidos] ...")
        return ""

    # ── 5. Análise com Opus 4.6 ───────────────────────────────────────────────
    logger.info(
        f"⑤ Enviando ao Claude Opus 4.6 "
        f"{'com Extended Thinking' if extended_thinking else ''}..."
    )
    analyzer = OpusAnalyzer(
        extended_thinking=extended_thinking,
        thinking_budget=thinking_budget,
    )
    result = analyzer.analyze(chunks, program_id=program.metadata.program_id)

    # ── 6. Saída ──────────────────────────────────────────────────────────────
    usage = result.token_usage.get("total", {})
    logger.info(
        f"✅ Análise concluída em {result.elapsed_seconds:.1f}s | "
        f"Tokens: {usage.get('input_tokens', 0):,} entrada / "
        f"{usage.get('output_tokens', 0):,} saída"
    )

    if output_path:
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(result.documentation, encoding='utf-8')
        logger.info(f"📄 Documentação salva em: {out}")
    else:
        print(result.documentation)

    return result.documentation


def _save_extracted_json(program, path: Path):
    """Serializa o programa extraído para JSON (para debug/auditoria)."""
    from dataclasses import asdict

    def clean(obj):
        if hasattr(obj, '__dict__'):
            return {k: clean(v) for k, v in obj.__dict__.items()}
        if isinstance(obj, list):
            return [clean(i) for i in obj]
        if hasattr(obj, 'value'):  # Enum
            return obj.value
        return obj

    data = clean(program)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')


# ──────────────────────────────────────────────────────────────────────────────
#  CLI
# ──────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="COBOL Business Rule Analyzer — powered by Claude Opus 4.6",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Exemplos:
  # Análise básica, saída no terminal
  python main.py analyze programa.cbl

  # Salva documentação em Markdown
  python main.py analyze programa.cbl --output docs/CALCJUR0.md

  # Com pasta de COPYbooks e Extended Thinking intensivo
  python main.py analyze programa.cbl --copybook-dir ./copy --thinking-budget 30000

  # Só extrai e mostra o payload (sem chamar a API)
  python main.py analyze programa.cbl --dry-run

  # Também salva o JSON extraído para auditoria
  python main.py analyze programa.cbl --output docs/CALC.md --save-extracted
        """
    )

    sub = parser.add_subparsers(dest="command")
    analyze = sub.add_parser("analyze", help="Analisa um programa COBOL")

    analyze.add_argument("source", help="Arquivo .cbl ou .cob")
    analyze.add_argument("--output", "-o", help="Arquivo .md de saída")
    analyze.add_argument(
        "--copybook-dir", "-c",
        action="append", dest="copybook_dirs", default=[],
        metavar="DIR",
        help="Pasta de COPYbooks (pode repetir para múltiplas pastas)"
    )
    analyze.add_argument(
        "--no-thinking", action="store_true",
        help="Desativa Extended Thinking (mais rápido, menos profundo)"
    )
    analyze.add_argument(
        "--thinking-budget", type=int, default=10_000,
        metavar="N",
        help="Tokens de raciocínio interno do Opus (padrão: 10000)"
    )
    analyze.add_argument(
        "--dry-run", action="store_true",
        help="Mostra o payload sem chamar a API"
    )
    analyze.add_argument(
        "--save-extracted", action="store_true",
        help="Salva o JSON extraído (.extracted.json) para auditoria"
    )

    args = parser.parse_args()

    if args.command == "analyze":
        run_pipeline(
            source_path=args.source,
            output_path=args.output,
            copybook_dirs=args.copybook_dirs,
            extended_thinking=not args.no_thinking,
            thinking_budget=args.thinking_budget,
            dry_run=args.dry_run,
            save_extracted=args.save_extracted,
        )
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
