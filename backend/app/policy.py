from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path


TOKEN_RE = re.compile(r"[a-z0-9]+")


@dataclass(frozen=True)
class PolicySection:
    key: str
    heading: str
    text: str
    tokens: tuple[str, ...]


class PolicySearch:
    """Small dependency-free BM25 index; each result is a complete policy section."""

    def __init__(self, source: Path) -> None:
        raw = source.read_text(encoding="utf-8")
        matches = list(re.finditer(r"(?m)^## (\d+)\. (.+)$", raw))
        self.sections: list[PolicySection] = []
        for index, match in enumerate(matches):
            end = matches[index + 1].start() if index + 1 < len(matches) else len(raw)
            text = raw[match.start():end].strip()
            self.sections.append(
                PolicySection(
                    key=match.group(1), heading=match.group(2), text=text,
                    tokens=tuple(self._tokens(text)),
                )
            )
        self.avg_len = sum(len(s.tokens) for s in self.sections) / max(len(self.sections), 1)
        self.doc_freq = Counter(token for s in self.sections for token in set(s.tokens))

    @staticmethod
    def _tokens(value: str) -> list[str]:
        return TOKEN_RE.findall(value.lower())

    def search(self, query: str, limit: int = 3) -> list[dict[str, str]]:
        terms = self._tokens(query)
        if not terms:
            return []
        query_terms = set(terms)
        scores: list[tuple[float, PolicySection]] = []
        corpus_size = len(self.sections)
        for section in self.sections:
            counts = Counter(section.tokens)
            score = 0.0
            matched = 0
            for term in query_terms:
                frequency = counts[term]
                if not frequency:
                    continue
                matched += 1
                idf = math.log(1 + (corpus_size - self.doc_freq[term] + 0.5) / (self.doc_freq[term] + 0.5))
                denominator = frequency + 1.5 * (1 - 0.75 + 0.75 * len(section.tokens) / self.avg_len)
                score += idf * frequency * 2.5 / denominator
            # Do not return unrelated "least bad" sections.
            if matched and (matched >= 2 or (len(query_terms) <= 2 and score >= 0.8)):
                scores.append((score, section))
        return [
            {"section": f"{section.key}. {section.heading}", "content": section.text}
            for _, section in sorted(scores, key=lambda item: item[0], reverse=True)[:limit]
        ]
