import { Bookmark, Check, Search } from "lucide-react";
import LoadNotice from "../components/LoadNotice";
import type { Skill } from "../lib/types";
import type { CatalogController } from "../hooks/useCatalog";
import type { SavedSkillsState } from "../hooks/useSavedSkills";

type LibraryViewProps = {
  view: "explore" | "saved";
  q: string;
  category: string;
  catalog: CatalogController;
  saved: SavedSkillsState;
  skillsById: Record<string, Skill>;
  matchBusy: boolean;
  onFilters: (filters: { q?: string; category?: string }, replace?: boolean) => void;
  onExplore: () => void;
  onOpenSkill: (skill: Skill) => void;
};

export default function LibraryView({
  view, q, category, catalog, saved, skillsById, matchBusy, onFilters, onExplore, onOpenSkill,
}: LibraryViewProps) {
  const { total: catalogTotal, hasMore, moreError, loadingMore } = catalog;
  const writeBusy = saved.busy;
  const savedIds = new Set(saved.items.map((item) => item.skill.id));
  const loadState = view === "saved" ? saved.loadState : catalog.loadState;
  // Explore uses server-wide categories/results; Saved is a complete collection filtered locally.
  const categories = view === "saved"
    ? [...new Set(saved.items.map((item) => item.skill.category))].sort()
    : catalog.categories;
  const visibleSkills = view === "explore" ? catalog.ids.map((id) => skillsById[id]) : saved.items.map((item) => item.skill).filter(
    (skill) => (category === "All skills" || skill.category === category) &&
      `${skill.name} ${skill.description} ${skill.author} ${skill.tags.join(" ")}`
        .toLowerCase().includes(q.toLowerCase()),
  );
  const hasFilters = Boolean(q) || category !== "All skills";
  const emptySaved = view === "saved" && loadState === "ready" && saved.items.length === 0 && !hasFilters;
  return (
    <section className="library">
      <div className="library-heading">
        <div>
          <h1>{view === "saved" ? "Saved skills" : "Explore skills"}</h1>
          <p>
            {view === "saved"
              ? "Skills you’ve saved, ready for your next project."
              : "Browse skills and save the ones that fit your work."}
          </p>
        </div>
      </div>
      <div className="library-filters" hidden={emptySaved}>
        <label className="search-field">
          <Search size={18} aria-hidden="true" />
          <span className="sr-only">Search skills</span>
          <input
            value={q}
            placeholder="Search skills, authors, or tags"
            onChange={(event) =>
              // Typing replaces history so Back does not step through every keystroke.
              onFilters({ q: event.target.value }, true)
            }
          />
        </label>
        <label className="category-select">
          <span className="sr-only">Category</span>
          <select
            value={category}
            onChange={(event) =>
              onFilters({ category: event.target.value })
            }
          >
            <option>All skills</option>
            {categories.map((item) => (
              <option key={item}>{item}</option>
            ))}
          </select>
        </label>
      </div>
      <LoadNotice label={view === "saved" ? "saved" : "catalog"} state={loadState} error={view === "saved" ? saved.loadError : catalog.loadError} onRetry={view === "saved" ? saved.reload : catalog.reload} />
      {view === "explore" && <LoadNotice label="saved" state={saved.loadState} error={saved.loadError} onRetry={saved.reload} />}
      {loadState === "loading" ? (
        <div className="empty-state" role="status">
          Loading skills…
        </div>
      ) : (
        loadState === "ready" && (
          <>
            {!emptySaved && <p className="result-count" role="status">
              {view === "explore" ? catalogTotal : visibleSkills.length}{" "}
              {(view === "explore" ? catalogTotal : visibleSkills.length) === 1 ? "skill" : "skills"}
              {category !== "All skills" ? ` in ${category}` : ""}
              {q ? ` matching “${q}”` : ""}
            </p>}
            <div className="skill-list">
              {visibleSkills.map((skill) => (
                <article className="catalog-row" key={skill.id}>
                  <div className="catalog-identity">
                    <h2>
                      <button
                        className="title-button"
                        title={skill.name}
                        onClick={() => onOpenSkill(skill)}
                      >
                        {skill.name}
                      </button>
                    </h2>
                    <span className="small muted">by {skill.author}</span>
                  </div>
                  <p className="catalog-description">{skill.description}</p>
                  <div className="catalog-meta">
                    {skill.category !== "Uncategorized" && <span>{skill.category}</span>}
                    {skill.tags.slice(0, 3).map((tag) => (
                      <span key={tag}>{tag}</span>
                    ))}
                  </div>
                  <div className="catalog-actions">
                    <button
                      className="text-button"
                      onClick={() => onOpenSkill(skill)}
                    >
                      View details
                    </button>
                    <button
                      className={
                        savedIds.has(skill.id)
                          ? "bookmark-button is-saved"
                          : "bookmark-button"
                      }
                      aria-label={
                        savedIds.has(skill.id)
                          ? `Remove ${skill.name} from saved`
                          : `Save ${skill.name}`
                      }
                      disabled={
                        writeBusy || matchBusy || saved.loadState !== "ready"
                      }
                      onClick={() => saved.bookmark(skill)}
                    >
                      {savedIds.has(skill.id) ? (
                        <Check size={17} aria-hidden="true" />
                      ) : (
                        <Bookmark size={17} aria-hidden="true" />
                      )}
                      <span>
                        {savedIds.has(skill.id) ? "Saved" : "Save"}
                      </span>
                    </button>
                  </div>
                </article>
              ))}
            </div>
            {view === "explore" && hasMore && (
              <div className="section-line">
                {moreError && <p className="error-notice" role="alert">{moreError}</p>}
                <button disabled={loadingMore} onClick={() => void catalog.loadMore()}>
                  {loadingMore ? "Loading more…" : moreError ? "Retry loading more" : "Load more"}
                </button>
              </div>
            )}
            {visibleSkills.length === 0 && (
              <div className="empty-state">
                <h2>
                  {hasFilters
                    ? "No skills match these filters."
                    : view === "saved" ? "No saved skills yet." : "No skills in the catalog yet."}
                </h2>
                <p>
                  {hasFilters
                    ? "Try another search or category."
                    : view === "saved" ? "Save skills from the catalog to find them here." : "Skills will appear here when they are available."}
                </p>
                {hasFilters ? (
                  <button
                    onClick={() =>
                      onFilters({ q: "", category: "All skills" })
                    }
                  >
                    Clear filters
                  </button>
                ) : view === "saved" && (
                  <button onClick={() => onExplore()}>
                    Explore skills
                  </button>
                )}
              </div>
            )}
          </>
        )
      )}
    </section>
  );
}
