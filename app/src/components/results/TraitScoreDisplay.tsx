import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";
import { TraitScore } from "@/types/search";
import { Info } from "lucide-react";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";

interface TraitScoreDisplayProps {
  traitName: string;
  traitScore: TraitScore;
  className?: string;
}

const formatVerticalSource = (source: string): string => {
  return source
    .split('_')
    .map(word => word.charAt(0).toUpperCase() + word.slice(1).toLowerCase())
    .join(' ');
};

export function TraitScoreDisplay({ traitName, traitScore, className }: TraitScoreDisplayProps) {
  const getScoreColorClasses = (score: number) => {
    if (score >= 0.8) return "bg-score-high text-score-high-foreground hover:bg-score-high hover:text-score-high-foreground";
    if (score >= 0.5) return "bg-score-medium text-score-medium-foreground hover:bg-score-medium hover:text-score-medium-foreground";
    return "bg-score-low text-score-low-foreground hover:bg-score-low hover:text-score-low-foreground";
  };

  const reasonText = traitScore.reason || traitScore.reasons?.[0] || "No reason provided";
  const verticalSources = traitScore.vertical_sources;

  return (
    <div className={cn("flex items-start gap-2 max-w-full", className)}>
      <Badge 
        variant="secondary" 
        className={cn("text-xs shrink-0 mt-0.5", getScoreColorClasses(traitScore.score))}
      >
        {traitScore.score >= 0.95 ? 100 : (traitScore.score * 100).toFixed(0)}%
      </Badge>
      <div className="min-w-0 flex-1">
        <span className="text-sm font-medium capitalize">{traitName}: </span>
        <span className="text-xs text-muted-foreground break-words">
          {reasonText}
        </span>
      </div>
      {verticalSources && verticalSources.length > 0 && (
        <Popover>
          <PopoverTrigger asChild>
            <button className="shrink-0 mt-0.5 text-muted-foreground hover:text-foreground transition-colors">
              <Info size={14} />
            </button>
          </PopoverTrigger>
          <PopoverContent side="left" align="start" className="w-auto max-w-[200px] p-3">
            <div className="space-y-1">
              <p className="text-xs font-medium text-foreground">Data Sources</p>
              <div className="flex flex-wrap gap-1">
                {verticalSources.map((source, idx) => (
                  <Badge key={idx} variant="secondary" className="text-xs">
                    {formatVerticalSource(source)}
                  </Badge>
                ))}
              </div>
            </div>
          </PopoverContent>
        </Popover>
      )}
    </div>
  );
}