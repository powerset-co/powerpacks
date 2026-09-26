import { useQueryClient } from "@tanstack/react-query";
import { useCallback, useRef, useState } from "react";

import type { ToastMessage } from "@/components/shared";
import { writeTags } from "@/lib/api/people";
import { plural } from "@/lib/people/copy";
import { nextTags, type TagAction } from "@/lib/people/facets";
import { EMPTY } from "@/lib/sets";
import type { Person, TagChange, TagResult } from "@/types/people";

import { PEOPLE_QUERY_KEY } from "./usePeopleQuery";

const DONE: Record<TagAction, (count: number) => string> = {
  share: (count) => `Marked ${plural(count, "person")} for sharing.`,
  private: (count) => `Marked ${plural(count, "person")} private.`,
  worth: (count) => `Removed your sharing choice for ${plural(count, "person")}.`,
};

function patchRows(rows: Person[] | undefined, results: readonly TagResult[]): Person[] | undefined {
  if (!rows) return rows;
  const byId = new Map(results.map((result) => [result.parent_id, result]));
  return rows.map((row) => {
    const patch = byId.get(row.parent_id);
    if (!patch) return row;
    return { ...row, share: patch.share, reason: patch.reason, share_source: patch.share_source, tags: patch.tags };
  });
}

function errorText(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

/**
 * Share / keep private / use worth for a set of people in one write. Rows are marked
 * pending while it runs and patched from the server's answer; undo re-posts the tags
 * each person held before.
 */
export function useDecisions(byId: ReadonlyMap<string, Person>, onWritten: (ids: readonly string[]) => void) {
  const queryClient = useQueryClient();
  const [pending, setPending] = useState<ReadonlySet<string>>(EMPTY);
  const [toast, setToast] = useState<ToastMessage | null>(null);
  const saving = useRef(false);
  const lastUndo = useRef<TagChange[] | null>(null);

  const write = useCallback(async (changes: TagChange[], undo: TagChange[] | null): Promise<number> => {
    const ids = changes.map((change) => change.parent_id);
    saving.current = true;
    setPending(new Set(ids));
    try {
      const results = await writeTags(changes);
      queryClient.setQueryData<Person[]>(PEOPLE_QUERY_KEY, (rows) => patchRows(rows, results));
      lastUndo.current = undo;
      onWritten(ids);
      return results.length;
    } finally {
      saving.current = false;
      setPending(EMPTY);
    }
  }, [queryClient, onWritten]);

  const undo = useCallback(async () => {
    const previous = lastUndo.current;
    if (saving.current || !previous) return;
    try {
      await write(previous, null);
      setToast({ message: `Undid changes for ${plural(previous.length, "person")}.` });
    } catch (error) {
      setToast({ message: `Couldn't undo. ${errorText(error)}`, error: true });
    }
  }, [write]);

  const apply = useCallback(async (action: TagAction, ids: readonly string[]) => {
    const rows = ids.map((id) => byId.get(id)).filter((row): row is Person => row !== undefined);
    if (saving.current || !rows.length) return;
    const changes = rows.map((row) => ({ parent_id: row.parent_id, tags: nextTags(row, action) }));
    const previous = rows.map((row) => ({ parent_id: row.parent_id, tags: [...row.tags] }));
    try {
      const count = await write(changes, previous);
      setToast({ message: DONE[action](count), action: { label: "Undo", kbd: "Z", onClick: () => void undo() } });
    } catch (error) {
      setToast({ message: `Couldn't save changes. ${errorText(error)}`, error: true });
    }
  }, [byId, write, undo]);

  const dismissToast = useCallback(() => setToast(null), []);

  return { pending, saving: pending.size > 0, apply, undo, toast, dismissToast };
}
