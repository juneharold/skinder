import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../lib/api";
import type { Skill } from "../lib/types";
import { errorMessage, type LoadState } from "../lib/async";

type CatalogOptions = {
  q: string;
  category: string;
  cacheSkills: (skills: Skill[]) => void;
};

export type CatalogController = {
  ids: string[];
  categories: string[];
  total: number;
  hasMore: boolean;
  loadingMore: boolean;
  moreError: string;
  loadState: LoadState;
  loadError: string;
  reload: () => Promise<void>;
  loadMore: () => Promise<void>;
};

export function useCatalog({ q, category, cacheSkills }: CatalogOptions): CatalogController {
  const [ids, setIds] = useState<string[]>([]);
  const [categories, setCategories] = useState<string[]>([]);
  const [total, setTotal] = useState(0);
  const [hasMore, setHasMore] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const [moreError, setMoreError] = useState("");
  const [loadState, setLoadState] = useState<LoadState>("loading");
  const [loadError, setLoadError] = useState("");
  const abort = useRef<AbortController | null>(null);
  const version = useRef(0);
  const filterKey = JSON.stringify([q, category]);
  const initialFilter = useRef(filterKey);
  const currentFilter = useRef(filterKey);
  currentFilter.current = filterKey;

  const load = useCallback(async (offset = 0) => {
    const requestVersion = ++version.current;
    abort.current?.abort();
    const controller = new AbortController();
    abort.current = controller;
    // Filters change before the debounced request starts; reject old pages in that gap.
    const current = () => !controller.signal.aborted &&
      requestVersion === version.current && currentFilter.current === filterKey;
    if (offset) setLoadingMore(true);
    else setLoadState("loading");
    setMoreError("");
    try {
      const next = await api.catalog({
        q, category: category === "All skills" ? undefined : category, offset, limit: 50,
      }, controller.signal);
      if (!current()) return;
      // Populate details before publishing IDs; the library resolves every ID through this cache.
      cacheSkills(next.skills);
      setIds((previous) => [...new Set([
        ...(offset ? previous : []), ...next.skills.map((skill) => skill.id),
      ])]);
      // A single page cannot provide the complete filter choices or result count.
      setCategories(next.categories);
      setTotal(next.total ?? 0);
      setHasMore(next.has_more ?? false);
      setLoadState("ready");
      setLoadError("");
    } catch (error) {
      if (!current()) return;
      if (offset) {
        // Keep previously loaded cards usable while offering a retry for the missing page.
        setMoreError(errorMessage(error));
        return;
      }
      setLoadState("error");
      setLoadError(errorMessage(error));
    } finally {
      if (current()) setLoadingMore(false);
    }
  }, [q, category, filterKey, cacheSkills]);

  useEffect(() => {
    setLoadState("loading");
    setLoadingMore(false);
    let timer: number | undefined;
    if (initialFilter.current === filterKey) void load();
    else timer = window.setTimeout(() => void load(), 250);
    return () => {
      clearTimeout(timer);
      abort.current?.abort();
    };
  }, [filterKey, load]);

  return {
    ids, categories, total, hasMore, loadingMore, moreError, loadState, loadError,
    reload: () => load(), loadMore: () => load(ids.length),
  };
}
