/**
 * Hook to manage tagged results per conversation.
 *
 * Each conversation has:
 *   - tags: an ordered, canonical list of tag names (preserved even with no
 *     assignments so the dropdown can list them).
 *   - assignments: personId → tag names. A person is "tagged" iff it has
 *     ≥1 entry in `assignments`.
 *
 * Persists to localStorage (key `powerset_tagged_${conversationId}`) so
 * tags survive page refresh. Auto-migrates the legacy `powerset_pinned_*`
 * key (a flat list of personIds) into a single "Pinned" tag the first time
 * a tagged conversation is read.
 *
 * Replaces the previous `usePinnedResults` hook.
 */

import { useState, useCallback, useMemo } from "react";

const STORAGE_PREFIX = "powerset_tagged_";
const LEGACY_PINNED_PREFIX = "powerset_pinned_";
const TAG_NAME_MAX = 40;
const LEGACY_PIN_TAG = "Pinned";

export interface TaggedData {
  /** Ordered list of tag names available in this conversation. */
  tags: string[];
  /** personId → list of tag names (subset of `tags`). */
  assignments: Record<string, string[]>;
}

function storageKey(conversationId: string): string {
  return `${STORAGE_PREFIX}${conversationId}`;
}

function legacyKey(conversationId: string): string {
  return `${LEGACY_PINNED_PREFIX}${conversationId}`;
}

function readTagged(conversationId: string | null): TaggedData {
  if (!conversationId) return { tags: [], assignments: {} };

  // Try the canonical key first.
  try {
    const raw = localStorage.getItem(storageKey(conversationId));
    if (raw) {
      const parsed = JSON.parse(raw);
      if (
        parsed &&
        Array.isArray(parsed.tags) &&
        parsed.assignments &&
        typeof parsed.assignments === "object"
      ) {
        return { tags: parsed.tags, assignments: parsed.assignments };
      }
    }
  } catch {
    /* fall through to legacy migration */
  }

  // One-time migration from the legacy "pinned" key.
  try {
    const oldRaw = localStorage.getItem(legacyKey(conversationId));
    if (oldRaw) {
      const oldIds = JSON.parse(oldRaw);
      if (Array.isArray(oldIds) && oldIds.length > 0) {
        const assignments: Record<string, string[]> = {};
        for (const id of oldIds) {
          if (typeof id === "string") assignments[id] = [LEGACY_PIN_TAG];
        }
        const migrated: TaggedData = { tags: [LEGACY_PIN_TAG], assignments };
        localStorage.setItem(storageKey(conversationId), JSON.stringify(migrated));
        localStorage.removeItem(legacyKey(conversationId));
        return migrated;
      }
    }
  } catch {
    /* ignore */
  }

  return { tags: [], assignments: {} };
}

function writeTagged(conversationId: string, data: TaggedData): void {
  if (data.tags.length === 0 && Object.keys(data.assignments).length === 0) {
    localStorage.removeItem(storageKey(conversationId));
  } else {
    localStorage.setItem(storageKey(conversationId), JSON.stringify(data));
  }
}

function normalizeTag(raw: string): string {
  return raw.trim().slice(0, TAG_NAME_MAX);
}

function findExisting(tags: string[], candidate: string): string | undefined {
  const lower = candidate.toLowerCase();
  return tags.find((t) => t.toLowerCase() === lower);
}

export function useTaggedResults(conversationId: string | null) {
  const [data, setData] = useState<TaggedData>(() => readTagged(conversationId));

  // Sync when conversationId changes (no useEffect — keeps hook synchronous
  // with first render of a switched conversation, matching the prior
  // usePinnedResults pattern).
  const [lastConvId, setLastConvId] = useState(conversationId);
  if (conversationId !== lastConvId) {
    setLastConvId(conversationId);
    setData(readTagged(conversationId));
  }

  const persist = useCallback(
    (next: TaggedData): TaggedData => {
      if (conversationId) writeTagged(conversationId, next);
      return next;
    },
    [conversationId]
  );

  const taggedIds = useMemo(() => {
    const set = new Set<string>();
    for (const [pid, tags] of Object.entries(data.assignments)) {
      if (tags.length > 0) set.add(pid);
    }
    return set;
  }, [data.assignments]);

  const isTagged = useCallback(
    (personId: string) => taggedIds.has(personId),
    [taggedIds]
  );

  const getTagsFor = useCallback(
    (personId: string): string[] => data.assignments[personId] ?? [],
    [data.assignments]
  );

  /**
   * Toggle a tag on a person. Creates the tag (case-insensitive dedupe) if it
   * doesn't exist yet. Removes the person's row from `assignments` when their
   * last tag is removed.
   */
  const toggleTag = useCallback(
    (personId: string, rawTag: string) => {
      if (!conversationId) return;
      const tag = normalizeTag(rawTag);
      if (!tag) return;
      setData((prev) => {
        const canonical = findExisting(prev.tags, tag) ?? tag;
        const tags = prev.tags.includes(canonical) ? prev.tags : [...prev.tags, canonical];
        const current = prev.assignments[personId] ?? [];
        const has = current.includes(canonical);
        const nextForPerson = has
          ? current.filter((t) => t !== canonical)
          : [...current, canonical];
        const nextAssignments = { ...prev.assignments };
        if (nextForPerson.length === 0) delete nextAssignments[personId];
        else nextAssignments[personId] = nextForPerson;
        return persist({ tags, assignments: nextAssignments });
      });
    },
    [conversationId, persist]
  );

  /** Add a tag to the canonical list without applying it to anyone. */
  const addTag = useCallback(
    (rawTag: string): string | null => {
      if (!conversationId) return null;
      const tag = normalizeTag(rawTag);
      if (!tag) return null;
      let resolved: string | null = tag;
      setData((prev) => {
        const existing = findExisting(prev.tags, tag);
        if (existing) {
          resolved = existing;
          return prev;
        }
        return persist({ ...prev, tags: [...prev.tags, tag] });
      });
      return resolved;
    },
    [conversationId, persist]
  );

  /** Remove a tag from the conversation entirely (drops it from every person). */
  const removeTag = useCallback(
    (rawTag: string) => {
      if (!conversationId) return;
      const tag = normalizeTag(rawTag);
      if (!tag) return;
      setData((prev) => {
        const canonical = findExisting(prev.tags, tag);
        if (!canonical) return prev;
        const tags = prev.tags.filter((t) => t !== canonical);
        const assignments: Record<string, string[]> = {};
        for (const [pid, list] of Object.entries(prev.assignments)) {
          const next = list.filter((t) => t !== canonical);
          if (next.length > 0) assignments[pid] = next;
        }
        return persist({ tags, assignments });
      });
    },
    [conversationId, persist]
  );

  /** Remove all tag assignments for the given personIds (tags themselves stay). */
  const untagAll = useCallback(
    (personIds: string[]) => {
      if (!conversationId || personIds.length === 0) return;
      setData((prev) => {
        const assignments = { ...prev.assignments };
        let changed = false;
        for (const id of personIds) {
          if (id in assignments) {
            delete assignments[id];
            changed = true;
          }
        }
        if (!changed) return prev;
        return persist({ ...prev, assignments });
      });
    },
    [conversationId, persist]
  );

  /** Wipe everything — all tags + all assignments. */
  const clearAll = useCallback(() => {
    if (!conversationId) return;
    setData(persist({ tags: [], assignments: {} }));
  }, [conversationId, persist]);

  return {
    tags: data.tags,
    assignments: data.assignments,
    taggedIds,
    isTagged,
    getTagsFor,
    toggleTag,
    addTag,
    removeTag,
    untagAll,
    clearAll,
    count: taggedIds.size,
  };
}

/** Clean up localStorage when a conversation is deleted. */
export function clearTaggedForConversation(conversationId: string): void {
  localStorage.removeItem(`${STORAGE_PREFIX}${conversationId}`);
  localStorage.removeItem(`${LEGACY_PINNED_PREFIX}${conversationId}`);
}
