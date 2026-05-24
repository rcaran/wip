"""
Pré-processador de COBOL.

Responsabilidades:
  - Normalizar o formato de colunas fixas (cols 1-6 = seq, 7 = indicador, 8-72 = código)
  - Remover/capturar comentários
  - Resolver e expandir COPYbooks
  - Unir linhas com continuação (col 7 = '-')
"""
from __future__ import annotations
import re
from pathlib import Path


SEQUENCE_AREA = slice(0, 6)
INDICATOR_COL = 6         # índice 6 = coluna 7 no COBOL
CODE_AREA = slice(7, 72)
COMMENT_INDICATORS = {'*', '/'}
CONTINUATION_INDICATOR = '-'
DEBUG_INDICATOR = 'D'


class PreprocessedLine:
    __slots__ = ('original', 'content', 'line_number', 'is_comment', 'comment_text', 'is_continued')

    def __init__(self, original: str, line_number: int):
        self.original = original
        self.line_number = line_number
        self.is_comment = False
        self.is_continued = False
        self.comment_text = ''
        self.content = ''
        self._parse(original)

    def _parse(self, raw: str):
        # Normaliza para pelo menos 72 chars
        padded = raw.rstrip('\n\r').ljust(72)

        indicator = padded[INDICATOR_COL] if len(padded) > INDICATOR_COL else ' '

        if indicator in COMMENT_INDICATORS:
            self.is_comment = True
            self.comment_text = padded[CODE_AREA].strip()
            return

        if indicator == CONTINUATION_INDICATOR:
            self.is_continued = True

        # Remove inline comment após área de código (col 73+)
        code = padded[CODE_AREA]
        self.content = code.rstrip()


class Preprocessor:
    """
    Prepara o código-fonte COBOL para o parser.
    """

    def __init__(self, copybook_dirs: list[str | Path] | None = None):
        self.copybook_dirs: list[Path] = [
            Path(d) for d in (copybook_dirs or [])
        ]
        self._copybook_cache: dict[str, list[PreprocessedLine]] = {}

    # ------------------------------------------------------------------ #
    #  API pública
    # ------------------------------------------------------------------ #

    def process(self, source: str | Path) -> tuple[list[PreprocessedLine], list[str]]:
        """
        Processa um arquivo COBOL completo.

        Retorna:
          - lista de PreprocessedLine já com COPYbooks expandidos
          - lista de nomes de copybooks referenciados
        """
        path = Path(source)
        raw_lines = path.read_text(encoding='latin-1', errors='replace').splitlines()
        return self._process_lines(raw_lines, source_path=path)

    def process_text(self, text: str) -> tuple[list[PreprocessedLine], list[str]]:
        """Processa COBOL fornecido como string (útil para testes)."""
        raw_lines = text.splitlines()
        return self._process_lines(raw_lines, source_path=None)

    # ------------------------------------------------------------------ #
    #  Internos
    # ------------------------------------------------------------------ #

    def _process_lines(
        self,
        raw_lines: list[str],
        source_path: Path | None
    ) -> tuple[list[PreprocessedLine], list[str]]:

        parsed: list[PreprocessedLine] = []
        copybooks_used: list[str] = []

        i = 0
        while i < len(raw_lines):
            line = PreprocessedLine(raw_lines[i], line_number=i + 1)
            parsed.append(line)

            # Detecta e expande COPY
            copy_name = self._detect_copy(line.content)
            if copy_name:
                copybooks_used.append(copy_name)
                expanded = self._expand_copybook(copy_name, source_path)
                if expanded:
                    # Insere linhas do copybook como linhas anotadas
                    parsed.extend(expanded)

            i += 1

        # Une linhas de continuação
        joined = self._join_continuations(parsed)
        return joined, list(dict.fromkeys(copybooks_used))  # preserva ordem, sem duplicatas

    _COPY_RE = re.compile(
        r'\bCOPY\s+([A-Z0-9#@$-]+)\b',
        re.IGNORECASE
    )

    def _detect_copy(self, content: str) -> str | None:
        m = self._COPY_RE.search(content)
        return m.group(1).upper() if m else None

    def _expand_copybook(self, name: str, source_path: Path | None) -> list[PreprocessedLine]:
        if name in self._copybook_cache:
            return self._copybook_cache[name]

        search_dirs = list(self.copybook_dirs)
        if source_path:
            search_dirs.insert(0, source_path.parent)

        extensions = ['', '.cpy', '.CPY', '.cbl', '.CBL', '.copy']
        for d in search_dirs:
            for ext in extensions:
                candidate = d / f"{name}{ext}"
                if candidate.exists():
                    raw = candidate.read_text(encoding='latin-1', errors='replace').splitlines()
                    lines = [PreprocessedLine(l, i + 1) for i, l in enumerate(raw)]
                    self._copybook_cache[name] = lines
                    return lines

        # Copybook não encontrado — retorna placeholder comentado
        placeholder = PreprocessedLine(
            f"      * [COPYBOOK NÃO ENCONTRADO: {name}]",
            line_number=0
        )
        placeholder.is_comment = True
        placeholder.comment_text = f"[COPYBOOK NÃO ENCONTRADO: {name}]"
        return [placeholder]

    @staticmethod
    def _join_continuations(lines: list[PreprocessedLine]) -> list[PreprocessedLine]:
        """Une linhas marcadas com continuação à linha anterior."""
        result: list[PreprocessedLine] = []
        for line in lines:
            if line.is_continued and result:
                # Remove aspas de abertura na linha continuada, se existir
                cont_content = line.content.lstrip()
                if cont_content.startswith(("'", '"')):
                    cont_content = cont_content[1:]
                result[-1].content = result[-1].content.rstrip() + cont_content
            else:
                result.append(line)
        return result
