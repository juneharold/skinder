import type { components } from "./api-schema";

// Keep UI imports independent of the generated OpenAPI module's internal schema paths.
export type Skill = components["schemas"]["Skill"];
export type Catalog = components["schemas"]["Catalog"];
export type Profile = components["schemas"]["Profile"];
export type Agent = Profile["agent"];
export type Recommendation = components["schemas"]["Recommendation"];
export type Feedback = components["schemas"]["Feedback"];
export type PassReason = NonNullable<Feedback["reason"]>;
export type MatchSession = components["schemas"]["MatchSession"];
export type Engine = MatchSession["engine"];
export type Health = components["schemas"]["Health"];
export type SavedItem = components["schemas"]["SavedItem"];
export type Saved = components["schemas"]["Saved"];
export type FeedbackResult = components["schemas"]["FeedbackResult"];
// Defaults give controls a complete shape while loading; matching still requires a loaded profile.
export const defaultProfile: Profile = {
  agent: "codex",
  planning: "balanced",
  communication: "balanced",
  verification: "balanced",
  output_style: "",
};
