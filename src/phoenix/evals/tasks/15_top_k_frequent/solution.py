from collections import Counter


def top_k_frequent(words, k):
    counts = Counter(words)
    return sorted(counts, key=lambda w: (-counts[w], w))[:k]
