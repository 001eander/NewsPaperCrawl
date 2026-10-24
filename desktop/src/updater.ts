export interface UpdateInfo {
  version: string;
  body: string | null;
  date: string | null;
}

export interface UpdateDownloadEvent {
  event: 'Started' | 'Progress' | 'Finished' | string;
  data?: {
    contentLength?: number;
    chunkLength?: number;
    [key: string]: unknown;
  };
}

export interface UpdateHandle extends UpdateInfo {
  downloadAndInstall(
    onEvent: (event: UpdateDownloadEvent) => void,
  ): Promise<void>;
}

export type UpdaterPhase =
  | 'idle'
  | 'checking'
  | 'up-to-date'
  | 'available'
  | 'blocked'
  | 'preparing'
  | 'downloading'
  | 'installing'
  | 'restarting'
  | 'error';

export interface UpdateProgress {
  downloaded: number;
  total: number | null;
  percent: number | null;
}

export interface UpdaterState {
  phase: UpdaterPhase;
  silent: boolean;
  update: UpdateInfo | null;
  progress: UpdateProgress | null;
  error: string | null;
  checkedAt: string | null;
}

export interface UpdaterDependencies {
  check(): Promise<UpdateHandle | null>;
  prepareUpdate(): Promise<void>;
  relaunch(): Promise<void>;
  getOperation(): Promise<string | null> | string | null;
  log?(level: 'info' | 'warn' | 'error', message: string): void;
  now?(): Date;
}

type UpdaterListener = (state: Readonly<UpdaterState>) => void;

const BUSY_PHASES: ReadonlySet<UpdaterPhase> = new Set<UpdaterPhase>([
  'checking',
  'preparing',
  'downloading',
  'installing',
  'restarting',
]);

function messageOf(error: unknown): string {
  if (error instanceof Error && error.message) {
    return error.message;
  }
  if (typeof error === 'string' && error) {
    return error;
  }
  if (error && typeof error === 'object' && 'message' in error) {
    const message = (error as { message?: unknown }).message;
    if (typeof message === 'string' && message) {
      return message;
    }
  }
  return error === undefined || error === null ? '未知错误' : String(error);
}

function clampPercent(value: number): number {
  return Math.min(100, Math.max(0, value));
}

export class UpdateController {
  private state: UpdaterState = {
    phase: 'idle',
    silent: false,
    update: null,
    progress: null,
    error: null,
    checkedAt: null,
  };

  private readonly listeners = new Set<UpdaterListener>();

  private updateHandle: UpdateHandle | null = null;

  constructor(private readonly deps: UpdaterDependencies) {}

  getState(): Readonly<UpdaterState> {
    return this.state;
  }

  subscribe(listener: UpdaterListener): () => void {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  }

  /**
   * 检查更新。silent 用于启动时检查：失败只写日志，不进入 error 状态，
   * 避免启动瞬间弹出与用户当前操作无关的错误。
   */
  async check(options: { silent?: boolean } = {}): Promise<void> {
    const silent = options.silent ?? false;
    if (BUSY_PHASES.has(this.state.phase)) {
      return;
    }

    this.patch({
      phase: 'checking',
      silent,
      error: null,
      progress: null,
    });

    const checkedAt = this.now().toISOString();

    try {
      const update = await this.deps.check();
      if (!update) {
        this.updateHandle = null;
        this.patch({
          phase: 'up-to-date',
          update: null,
          checkedAt,
        });
        return;
      }
      this.updateHandle = update;
      this.patch({
        phase: 'available',
        update: {
          version: update.version,
          body: update.body ?? null,
          date: update.date ?? null,
        },
        checkedAt,
      });
    } catch (error) {
      const message = `检查更新失败：${messageOf(error)}`;
      if (silent) {
        this.deps.log?.('warn', message);
        this.patch({
          phase: 'idle',
          error: null,
          checkedAt,
        });
      } else {
        this.patch({
          phase: 'error',
          error: message,
          checkedAt,
        });
      }
    }
  }

  /**
   * 安装更新。必须先确认没有活动任务，再调用 prepare_update；
   * 只有 prepare_update 成功关闭 sidecar 后才开始下载安装。
   */
  async install(): Promise<boolean> {
    const update = this.updateHandle;
    if (!update || !this.state.update) {
      this.patch({
        phase: 'error',
        error: '没有可安装的更新，请先检查更新。',
      });
      return false;
    }
    if (BUSY_PHASES.has(this.state.phase)) {
      return false;
    }

    let operation: string | null;
    try {
      operation = await this.deps.getOperation();
    } catch (error) {
      this.patch({
        phase: 'error',
        error: `无法确认当前任务状态：${messageOf(error)}`,
      });
      return false;
    }

    if (operation) {
      const message = `当前有任务正在运行（${operation}），请先停止任务或等待完成后再安装更新。`;
      this.deps.log?.('warn', message);
      this.patch({ phase: 'blocked', error: message });
      return false;
    }

    this.patch({
      phase: 'preparing',
      error: null,
      progress: { downloaded: 0, total: null, percent: null },
    });

    try {
      await this.deps.prepareUpdate();
    } catch (error) {
      this.patch({
        phase: 'error',
        error: `无法进入更新安装流程：${messageOf(error)}`,
      });
      return false;
    }

    this.patch({ phase: 'downloading' });

    try {
      await update.downloadAndInstall((event) => {
        this.handleDownloadEvent(event);
      });
      this.patch({ phase: 'installing' });
      await this.deps.relaunch();
      this.patch({ phase: 'restarting' });
      return true;
    } catch (error) {
      this.patch({
        phase: 'error',
        error:
          `下载或安装更新失败：${messageOf(error)}` +
          '；后端可能已关闭，请重启应用后重试。',
      });
      return false;
    }
  }

  private handleDownloadEvent(event: UpdateDownloadEvent): void {
    if (event.event === 'Started') {
      const length = event.data?.contentLength;
      const total =
        typeof length === 'number' && Number.isFinite(length) && length > 0
          ? length
          : null;
      this.patch({
        phase: 'downloading',
        progress: { downloaded: 0, total, percent: total ? 0 : null },
      });
      return;
    }

    if (event.event === 'Progress') {
      const chunk = event.data?.chunkLength;
      if (typeof chunk !== 'number' || !Number.isFinite(chunk) || chunk < 0) {
        return;
      }
      const current = this.state.progress ?? {
        downloaded: 0,
        total: null,
        percent: null,
      };
      const downloaded = current.downloaded + chunk;
      const percent =
        current.total && current.total > 0
          ? clampPercent((downloaded / current.total) * 100)
          : null;
      this.patch({
        phase: 'downloading',
        progress: { downloaded, total: current.total, percent },
      });
      return;
    }

    if (event.event === 'Finished') {
      const current = this.state.progress;
      this.patch({
        phase: 'installing',
        progress: current
          ? {
              downloaded: current.total ?? current.downloaded,
              total: current.total,
              percent: current.total ? 100 : current.percent,
            }
          : { downloaded: 0, total: null, percent: 100 },
      });
    }
  }

  private patch(patch: Partial<UpdaterState>): void {
    this.state = { ...this.state, ...patch };
    for (const listener of this.listeners) {
      listener(this.state);
    }
  }

  private now(): Date {
    return this.deps.now?.() ?? new Date();
  }
}

export function createUpdateController(
  dependencies: UpdaterDependencies,
): UpdateController {
  return new UpdateController(dependencies);
}
