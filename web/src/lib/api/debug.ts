// The desktop app's debug menu (desktop/src-tauri/src/lib.rs): on in test builds only.

import { invoke } from "@/lib/desktop"
import { isRecord } from "@/lib/utils"

export interface DebugState {
  enabled: boolean
  setupSkipped: boolean
}

export async function fetchDebugState(): Promise<DebugState> {
  const state = await invoke("debug_state")
  return {
    enabled: isRecord(state) && state.enabled === true,
    setupSkipped: isRecord(state) && state.setupSkipped === true,
  }
}

/** Skip setup on later launches; turning the skip off resumes setup now. */
export async function skipSetup(skip: boolean): Promise<void> {
  await invoke("debug_skip_setup", { skip })
}

/** Forget the app's data and start setup over: the page server restarts on the empty folder. */
export async function resetData(): Promise<void> {
  await invoke("debug_reset_data")
}
