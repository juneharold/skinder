import {
  Braces,
  Feather,
  FlaskConical,
  ChartNoAxesCombined,
  Layers3,
  Megaphone,
  Palette,
  FileText,
  Sparkles,
} from "lucide-react";
export const categoryIcons: Record<string, typeof Sparkles> = {
  Development: Braces,
  Design: Palette,
  Writing: Feather,
  Research: FlaskConical,
  Documents: FileText,
  Data: ChartNoAxesCombined,
  Productivity: Layers3,
  Marketing: Megaphone,
};
export function SkillIcon({
  category,
  size = 28,
}: {
  category: string;
  size?: number;
}) {
  const Icon = categoryIcons[category] ?? Sparkles;
  return <Icon size={size} strokeWidth={1.7} aria-hidden="true" />;
}
