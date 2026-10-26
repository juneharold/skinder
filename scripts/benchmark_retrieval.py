"""Measure local USearch indexing/search, without downloading a model.

Synthetic clustered vectors measure the index, not skill relevance. Use --vectors with a
normalized float32 .npy matrix to measure a real corpus. First-query timing does not flush
the operating system's filesystem cache. Run separately from the automated test suite.
"""

import argparse
import json
import resource
import sys
import tempfile
import time
from pathlib import Path

import numpy as np
from usearch.index import Index, search


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=100_000)
    parser.add_argument("--queries", type=int, default=100)
    parser.add_argument("--vectors", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--expansion-search", type=int, default=256)
    args = parser.parse_args()
    rng = np.random.default_rng(42)
    if args.vectors:
        vectors = np.load(args.vectors, mmap_mode="r")
        kind = "provided .npy vectors; queries are sampled indexed vectors"
        queries = np.asarray(vectors[rng.choice(len(vectors), args.queries, replace=False)])
    else:
        centers = rng.normal(size=(1000, 384)).astype(np.float32)
        centers /= np.linalg.norm(centers, axis=1, keepdims=True)
        vectors = centers[rng.integers(len(centers), size=args.count)]
        vectors += rng.normal(0, 0.01, vectors.shape).astype(np.float32)
        vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)
        queries = centers[rng.integers(len(centers), size=args.queries)].copy()
        queries += rng.normal(0, 0.01, queries.shape).astype(np.float32)
        queries /= np.linalg.norm(queries, axis=1, keepdims=True)
        kind = "synthetic clustered vectors; held-out queries"
    k = min(100, len(vectors))
    with tempfile.TemporaryDirectory(prefix="skinder-vector-benchmark-") as directory:
        path = Path(directory) / "benchmark.usearch"
        started = time.perf_counter()
        index = Index(ndim=vectors.shape[1], metric="cos", dtype="f16", connectivity=16,
                      expansion_add=128, expansion_search=args.expansion_search)
        for start in range(0, len(vectors), 1024):
            batch = vectors[start:start + 1024]
            index.add(np.arange(start, start + len(batch), dtype=np.uint64), batch, threads=4)
        build_seconds = time.perf_counter() - started
        index.save(path)
        del index
        started = time.perf_counter()
        index = Index.restore(path, view=True)
        index.expansion_search = args.expansion_search
        map_seconds = time.perf_counter() - started
        started = time.perf_counter()
        index.search(queries[0], count=k, threads=1)
        first_seconds = time.perf_counter() - started
        times, recalls = [], []
        for query in queries:
            started = time.perf_counter()
            matches = index.search(query, count=k, threads=1)
            times.append(time.perf_counter() - started)
            exact = search(vectors, query, count=k, metric="cos", exact=True, threads=4)
            recalls.append(len(set(matches.keys) & set(exact.keys)) / k)
        memory = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        result = {
            "data": kind, "count": len(vectors), "dimensions": vectors.shape[1],
            "queries": len(queries), "expansion_search": args.expansion_search,
            "build_seconds": build_seconds, "map_seconds": map_seconds,
            "first_query_ms": first_seconds * 1000,
            "first_query_note": "Filesystem cache was not flushed; not a cold-disk guarantee.",
            "warm_query_ms_p50": float(np.percentile(times, 50) * 1000),
            "warm_query_ms_p95": float(np.percentile(times, 95) * 1000),
            "mean_recall_at_100": float(np.mean(recalls)),
            "recall_target": 0.95, "recall_target_met": bool(np.mean(recalls) >= 0.95),
            "index_bytes": path.stat().st_size,
            "peak_process_bytes": memory if sys.platform == "darwin" else memory * 1024,
            "memory_note": "Includes source vectors and exact-search reference work.",
        }
    rendered = json.dumps(result, indent=2)
    print(rendered)
    if args.output:
        args.output.write_text(rendered + "\n")


if __name__ == "__main__":
    main()
