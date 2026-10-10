import numpy as np
from langchain_core.embeddings import Embeddings


class SemanticChunker:
    """Groups consecutive text units (sentences or paragraphs) into topic chunks.

    Each unit is embedded together with its neighbours (``buffer_size`` on
    each side) to smooth out noise. The cosine distance between consecutive
    windows measures how much the topic changes; any gap whose distance is
    above the ``breakpoint_percentile`` of all gaps becomes a chunk boundary.
    Callers can also pass hard group boundaries (e.g. one group per service)
    that are never merged across.
    """

    def __init__(
        self,
        embeddings: Embeddings,
        buffer_size: int = 1,
        breakpoint_percentile: float = 80.0,
    ) -> None:
        self.embeddings = embeddings
        self.buffer_size = buffer_size
        self.breakpoint_percentile = breakpoint_percentile

    def _gap_distances(self, units: list[str]) -> np.ndarray:
        if len(units) < 2:
            return np.array([])
        windows = [
            " ".join(units[max(0, i - self.buffer_size) : i + self.buffer_size + 1])
            for i in range(len(units))
        ]
        vectors = np.array(self.embeddings.embed_documents(windows))
        vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)
        return 1.0 - np.sum(vectors[:-1] * vectors[1:], axis=1)

    def chunk_groups(self, groups: list[list[str]]) -> list[list[str]]:
        """Chunk each group of units; returns a list of chunks per group.

        The breakpoint threshold is computed over the gaps of all groups so it
        stays meaningful even when individual groups are only a few units.
        """
        distances = [self._gap_distances(group) for group in groups]
        all_gaps = np.concatenate(distances) if distances else np.array([])
        threshold = np.percentile(all_gaps, self.breakpoint_percentile) if all_gaps.size else 0.0

        chunked: list[list[str]] = []
        for group, gaps in zip(groups, distances):
            chunks: list[str] = []
            start = 0
            for i, distance in enumerate(gaps):
                if distance > threshold:
                    chunks.append(" ".join(group[start : i + 1]))
                    start = i + 1
            if start < len(group):
                chunks.append(" ".join(group[start:]))
            chunked.append(chunks)
        return chunked
