import { invoke, isTauri } from '@tauri-apps/api/core';
import { listen, type UnlistenFn } from '@tauri-apps/api/event';
import { errorMessage } from './dom';
import type {
  ConfigKind,
  CoreState,
  PathKind,
  RunParams,
  Stage,
} from './types';

export interface CoreEvent {
  type: string;
  message?: string;
  operation?: string;
  outcome?: string;
  [key: string]: unknown;
}

export const isDesktop = (() => {
  try {
    return isTauri();
  } catch {
    return false;
  }
})();

async function invokeRaw<T>(
  command: string,
  args?: Record<string, unknown>,
): Promise<T> {
  try {
    return await invoke<T>(command, args);
  } catch (error) {
    throw new Error(errorMessage(error));
  }
}

async function core<T>(request: Record<string, unknown>): Promise<T> {
  return invokeRaw<T>('core_request', { request });
}

export const api = {
  state(): Promise<CoreState> {
    return core<CoreState>({ command: 'state' });
  },

  status(): Promise<unknown> {
    return core<unknown>({ command: 'status' });
  },

  failed(limit = 5): Promise<unknown> {
    return core<unknown>({ command: 'failed', limit });
  },

  run(stage: Stage, params: RunParams): Promise<unknown> {
    return core<unknown>({ command: 'run', stage, params });
  },

  login(): Promise<unknown> {
    return core<unknown>({ command: 'login' });
  },

  cancel(): Promise<unknown> {
    return core<unknown>({ command: 'cancel' });
  },

  loadConfig(): Promise<unknown> {
    return core<unknown>({ command: 'load_config' });
  },

  saveConfig(
    kind: ConfigKind,
    values: Record<string, string>,
  ): Promise<unknown> {
    return core<unknown>({ command: 'save_config', kind, values });
  },

  testBark(): Promise<unknown> {
    return core<unknown>({ command: 'test_bark' });
  },

  shutdown(): Promise<unknown> {
    return core<unknown>({ command: 'shutdown' });
  },

  getPaths(): Promise<unknown> {
    return invokeRaw<unknown>('get_paths');
  },

  openPath(kind: PathKind): Promise<unknown> {
    return invokeRaw<unknown>('open_path', { kind });
  },

  prepareUpdate(): Promise<unknown> {
    return invokeRaw<unknown>('prepare_update');
  },
};

export type CoreApi = typeof api;

export async function listenCoreEvents(
  handler: (event: CoreEvent) => void,
): Promise<UnlistenFn> {
  if (!isDesktop) {
    throw new Error('当前不在 Tauri 桌面应用中，无法订阅后端事件');
  }
  return listen<CoreEvent>('core-event', (event) => {
    handler(event.payload);
  });
}
