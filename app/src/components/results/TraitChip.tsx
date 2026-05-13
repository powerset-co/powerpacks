/**
 * TraitChip — Interactive trait badge with temporal toggle + edit/remove.
 *
 * Displays a trait value with a visual indicator for its temporal scope
 * (current/past/all). Supports:
 * - Click temporal indicator to cycle/dropdown temporal scope
 * - Hover to reveal pencil (edit) + X (remove) icons
 * - Inline editing of trait value
 *
 * Created: 2026-03-26
 * Changelog:
 * - 2026-03-26: Added edit/remove support, TraitChipList wrapper
 */

import { useState, useRef, useEffect } from "react";
import { cn } from "@/lib/utils";
import { Badge } from "@/components/ui/badge";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Clock, History, Infinity as InfinityIcon, Pencil, X, Check, Plus } from "lucide-react";
import type { Trait, TraitTemporal } from "@/types/search";

// ── Temporal config ──

export const TEMPORAL_CONFIG: Record<TraitTemporal, {
  icon: typeof Clock;
  label: string;
  tooltip: string;
  badgeClass: string;
  indicatorClass: string;
}> = {
  current: {
    icon: Clock,
    label: "Current",
    tooltip: "Must match current role",
    badgeClass: "border-blue-400/50 bg-blue-500/10 text-blue-700 dark:border-blue-500/40 dark:bg-blue-500/15 dark:text-blue-300",
    indicatorClass: "bg-blue-500",
  },
  past: {
    icon: History,
    label: "Past",
    tooltip: "Past positions only",
    badgeClass: "border-amber-400/50 bg-amber-500/10 text-amber-700 dark:border-amber-500/40 dark:bg-amber-500/15 dark:text-amber-300",
    indicatorClass: "bg-amber-500",
  },
  all: {
    icon: InfinityIcon,
    label: "All",
    tooltip: "Entire profile (default)",
    badgeClass: "border-border bg-secondary/50 text-foreground",
    indicatorClass: "bg-muted-foreground/40",
  },
};

const CYCLE_ORDER: TraitTemporal[] = ["all", "current", "past"];

// ── TraitChip ──

interface TraitChipProps {
  trait: Trait;
  /** Called when the user changes the temporal scope. */
  onTemporalChange?: (temporal: TraitTemporal) => void;
  /** Called when the user edits the trait value. */
  onEdit?: (newValue: string) => void;
  /** Called when the user removes this trait. */
  onRemove?: () => void;
  /** "click" = cycle on click, "dropdown" = open dropdown menu. Default: "click" */
  mode?: "click" | "dropdown";
  /** Compact mode hides the temporal label text, shows only the icon. Default: false */
  compact?: boolean;
  className?: string;
}

export function TraitChip({
  trait,
  onTemporalChange,
  onEdit,
  onRemove,
  mode = "click",
  compact = false,
  className,
}: TraitChipProps) {
  const [isEditing, setIsEditing] = useState(false);
  const [editValue, setEditValue] = useState(trait.value);
  const inputRef = useRef<HTMLInputElement>(null);

  const config = TEMPORAL_CONFIG[trait.temporal];
  const Icon = config.icon;
  const hasTemporalToggle = !!onTemporalChange;
  const hasActions = !!onEdit || !!onRemove;

  useEffect(() => {
    if (isEditing && inputRef.current) {
      inputRef.current.focus();
      inputRef.current.select();
    }
  }, [isEditing]);

  const handleConfirmEdit = () => {
    const trimmed = editValue.trim();
    if (trimmed && trimmed !== trait.value) {
      onEdit?.(trimmed);
    }
    setIsEditing(false);
  };

  const handleCancelEdit = () => {
    setEditValue(trait.value);
    setIsEditing(false);
  };

  // ── Editing state ──
  if (isEditing) {
    return (
      <Badge
        variant="outline"
        className={cn(
          "gap-1 py-0.5 pl-2 pr-1 text-xs font-normal",
          config.badgeClass,
        )}
      >
        <input
          ref={inputRef}
          type="text"
          value={editValue}
          onChange={(e) => setEditValue(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") { e.preventDefault(); handleConfirmEdit(); }
            else if (e.key === "Escape") handleCancelEdit();
          }}
          onBlur={handleConfirmEdit}
          className="bg-transparent border-none outline-none text-xs min-w-[80px] w-auto"
          style={{ width: `${Math.max(80, editValue.length * 7)}px` }}
        />
        <button onClick={handleConfirmEdit} className="p-0.5 hover:bg-primary/20 rounded transition-colors">
          <Check className="h-3 w-3 text-primary" />
        </button>
        <button
          onClick={(e) => { e.preventDefault(); handleCancelEdit(); }}
          onMouseDown={(e) => e.preventDefault()}
          className="p-0.5 hover:bg-destructive/20 rounded transition-colors"
        >
          <X className="h-3 w-3 text-muted-foreground hover:text-destructive" />
        </button>
      </Badge>
    );
  }

  // ── Temporal indicator (the dot + icon) — clickable when has toggle ──
  const temporalIndicator = (
    <span
      className={cn(
        "flex items-center gap-1 shrink-0",
        hasTemporalToggle && "cursor-pointer hover:opacity-80",
      )}
      onClick={(e) => {
        if (!hasTemporalToggle) return;
        e.stopPropagation();
        if (mode === "click") {
          const currentIdx = CYCLE_ORDER.indexOf(trait.temporal);
          const nextIdx = (currentIdx + 1) % CYCLE_ORDER.length;
          onTemporalChange!(CYCLE_ORDER[nextIdx]);
        }
      }}
    >
      <span className={cn("w-1.5 h-1.5 rounded-full transition-colors", config.indicatorClass)} />
      <Icon size={12} className="opacity-70" />
    </span>
  );

  // Wrap temporal indicator in dropdown if mode === "dropdown"
  const temporalElement = mode === "dropdown" && hasTemporalToggle ? (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        {temporalIndicator}
      </DropdownMenuTrigger>
      <DropdownMenuContent align="start" className="w-44">
        {CYCLE_ORDER.map((temporal) => {
          const cfg = TEMPORAL_CONFIG[temporal];
          const ItemIcon = cfg.icon;
          return (
            <DropdownMenuItem
              key={temporal}
              onClick={() => onTemporalChange!(temporal)}
              className={cn("gap-2 text-xs", trait.temporal === temporal && "font-semibold")}
            >
              <span className={cn("w-2 h-2 rounded-full shrink-0", cfg.indicatorClass)} />
              <ItemIcon size={14} />
              <div className="flex flex-col">
                <span>{cfg.label}</span>
                <span className="text-[10px] text-muted-foreground">{cfg.tooltip}</span>
              </div>
            </DropdownMenuItem>
          );
        })}
      </DropdownMenuContent>
    </DropdownMenu>
  ) : temporalIndicator;

  // ── Normal badge ──
  const chipContent = (
    <div className={cn("group relative inline-flex", className)}>
      <Badge
        variant="outline"
        className={cn(
          "gap-1.5 py-1 px-2.5 text-xs font-normal transition-all",
          config.badgeClass,
        )}
      >
        {temporalElement}

        {/* Trait value — click to cycle temporal */}
        <span
          className={cn("truncate", hasTemporalToggle && "cursor-pointer")}
          onClick={(e) => {
            if (!hasTemporalToggle || mode !== "click") return;
            e.stopPropagation();
            const currentIdx = CYCLE_ORDER.indexOf(trait.temporal);
            const nextIdx = (currentIdx + 1) % CYCLE_ORDER.length;
            onTemporalChange!(CYCLE_ORDER[nextIdx]);
          }}
        >
          {trait.value}
        </span>

        {/* Temporal label (unless compact) */}
        {!compact && (
          <span className="text-[10px] font-medium uppercase tracking-wider opacity-60 shrink-0">
            {config.label}
          </span>
        )}

        {/* Hover-reveal edit + remove icons — 500ms collapse so chip doesn't snap shut */}
        {hasActions && (
          <span className="inline-flex items-center gap-0.5 max-w-0 overflow-hidden opacity-0 group-hover:max-w-[40px] group-hover:opacity-100 transition-all duration-500 group-hover:duration-150">
            {onEdit && (
              <button
                onClick={(e) => { e.stopPropagation(); setEditValue(trait.value); setIsEditing(true); }}
                className="p-0.5 hover:bg-primary/20 rounded transition-colors"
              >
                <Pencil className="h-3 w-3 text-muted-foreground hover:text-primary transition-colors" />
              </button>
            )}
            {onRemove && (
              <button
                onClick={(e) => { e.stopPropagation(); onRemove(); }}
                className="p-0.5 hover:bg-destructive/20 rounded transition-colors"
              >
                <X className="h-3 w-3 text-muted-foreground hover:text-destructive transition-colors" />
              </button>
            )}
          </span>
        )}
      </Badge>
    </div>
  );

  return chipContent;
}

// ── TraitChipList — manages a list of TraitChips with add/edit/remove ──

interface TraitChipListProps {
  traits: Trait[];
  onChange: (traits: Trait[]) => void;
  /** Interaction mode for temporal toggle. Default: "click" */
  mode?: "click" | "dropdown";
  /** Show compact chips. Default: false */
  compact?: boolean;
  /** Show the "+ Add" button. Default: true */
  showAdd?: boolean;
  /** Placeholder for the add input. Default: "Add trait..." */
  addPlaceholder?: string;
  className?: string;
}

export function TraitChipList({
  traits,
  onChange,
  mode = "click",
  compact = false,
  showAdd = true,
  addPlaceholder = "Add trait...",
  className,
}: TraitChipListProps) {
  const [isAdding, setIsAdding] = useState(false);
  const [addValue, setAddValue] = useState("");
  const addInputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (isAdding && addInputRef.current) {
      addInputRef.current.focus();
    }
  }, [isAdding]);

  const handleAdd = () => {
    const trimmed = addValue.trim();
    if (trimmed) {
      onChange([...traits, { value: trimmed, temporal: "all", meaning: "general" }]);
    }
    setAddValue("");
    setIsAdding(false);
  };

  const handleCancelAdd = () => {
    setAddValue("");
    setIsAdding(false);
  };

  return (
    <div className={cn("flex flex-wrap items-center gap-1.5", className)}>
      {traits.map((trait, i) => (
        <TraitChip
          key={`${trait.value}-${i}`}
          trait={trait}
          mode={mode}
          compact={compact}
          onTemporalChange={(temporal) => {
            const updated = [...traits];
            updated[i] = { ...trait, temporal };
            onChange(updated);
          }}
          onEdit={(newValue) => {
            const updated = [...traits];
            updated[i] = { ...trait, value: newValue };
            onChange(updated);
          }}
          onRemove={() => {
            onChange(traits.filter((_, idx) => idx !== i));
          }}
        />
      ))}

      {/* Add new trait */}
      {showAdd && isAdding && (
        <Badge variant="outline" className="gap-1 py-0.5 pl-2 pr-1 text-xs font-normal border-dashed">
          <input
            ref={addInputRef}
            type="text"
            value={addValue}
            onChange={(e) => setAddValue(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") { e.preventDefault(); handleAdd(); }
              else if (e.key === "Escape") handleCancelAdd();
            }}
            onBlur={handleAdd}
            placeholder={addPlaceholder}
            className="bg-transparent border-none outline-none text-xs min-w-[80px] w-auto placeholder:text-muted-foreground/50"
            style={{ width: `${Math.max(80, addValue.length * 7)}px` }}
          />
          <button onClick={handleAdd} className="p-0.5 hover:bg-primary/20 rounded transition-colors">
            <Check className="h-3 w-3 text-primary" />
          </button>
          <button
            onClick={(e) => { e.preventDefault(); handleCancelAdd(); }}
            onMouseDown={(e) => e.preventDefault()}
            className="p-0.5 hover:bg-destructive/20 rounded transition-colors"
          >
            <X className="h-3 w-3 text-muted-foreground hover:text-destructive" />
          </button>
        </Badge>
      )}

      {showAdd && !isAdding && (
        <Badge
          variant="outline"
          className="gap-1 py-1 px-2.5 text-xs font-normal cursor-pointer hover:bg-accent transition-colors border-dashed text-muted-foreground"
          onClick={() => setIsAdding(true)}
        >
          <Plus className="h-3 w-3" />
          Add
        </Badge>
      )}
    </div>
  );
}
