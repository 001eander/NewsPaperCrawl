import type { AppContext } from '../context';
import { byId, formatBytes } from '../dom';
import type { UpdateController, UpdaterState } from '../updater';

const STARTUP_KEY = 'newspaper.updates.checkOnStartup';

export interface AboutViewApi {
  setActive(active: boolean): void;
  startupCheck(): void;
}

function formatReleaseDate(value: string | null): string {
  if (!value) {
    return '';
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value;
  }
  return date.toLocaleDateString('zh-CN', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
  });
}

export function createAboutView(
  ctx: AppContext,
  updater: UpdateController,
): AboutViewApi {
  const aboutVersion = byId<HTMLElement>('about-version');
  const infoVersion = byId<HTMLElement>('info-version');
  const updateStatus = byId<HTMLDivElement>('update-status');
  const updateDot = byId<HTMLElement>('update-dot');
  const updateTitle = byId<HTMLElement>('update-title');
  const updateDetail = byId<HTMLElement>('update-detail');
  const progressBox = byId<HTMLDivElement>('update-progress');
  const progressBar = byId<HTMLElement>('update-progress-bar');
  const progressText = byId<HTMLElement>('update-progress-text');
  const notes = byId<HTMLElement>('update-notes');
  const operationHint = byId<HTMLParagraphElement>('update-operation-hint');
  const updateError = byId<HTMLParagraphElement>('update-error');
  const startupCheckBox = byId<HTMLInputElement>('update-startup-check');
  const checkButton = byId<HTMLButtonElement>('update-check');
  const installButton = byId<HTMLButtonElement>('update-install');

  let lastState: Readonly<UpdaterState> = updater.getState();
  let startupCheckDone = false;

  try {
    if (window.localStorage.getItem(STARTUP_KEY) === '0') {
      startupCheckBox.checked = false;
    }
  } catch {
    // localStorage 不可用时保持默认开启
  }

  startupCheckBox.addEventListener('change', () => {
    try {
      window.localStorage.setItem(STARTUP_KEY, startupCheckBox.checked ? '1' : '0');
    } catch {
      // 忽略持久化失败
    }
  });

  const currentVersion = (): string => ctx.store.get().version;

  const updateOperationHint = (
    state: Readonly<UpdaterState>,
    operation: string | null,
  ): void => {
    const showHint = Boolean(operation && state.update);
    operationHint.hidden = !showHint;
    operationHint.textContent = showHint
      ? `当前任务（${ctx.operationLabel(operation, ctx.store.get().stage)}）仍在运行，安装更新前需要先停止或等待完成。`
      : '';
  };

  const render = (state: Readonly<UpdaterState>): void => {
    lastState = state;
    const version = currentVersion();
    const update = state.update;
    let title = '';
    let detail = '';

    switch (state.phase) {
      case 'idle':
        title = '尚未检查更新';
        detail = `当前版本 v${version}`;
        break;
      case 'checking':
        title = '正在检查更新…';
        detail = state.silent ? '启动静默检查更新' : '正在联系更新服务器';
        break;
      case 'up-to-date':
        title = '已是最新版本';
        detail = state.silent
          ? `启动检查完成，当前版本 v${version}`
          : `当前版本 v${version}`;
        break;
      case 'available':
        title = `发现新版本 v${update?.version ?? ''}`;
        detail = [
          `当前版本 v${version}`,
          update?.date ? `发布于 ${formatReleaseDate(update.date)}` : '',
        ]
          .filter(Boolean)
          .join(' · ');
        break;
      case 'blocked':
        title = '暂时无法安装更新';
        detail = state.error ?? '有任务正在运行，请稍后再试。';
        break;
      case 'preparing':
        title = '正在准备安装…';
        detail = '正在等待后端安全关闭。';
        break;
      case 'downloading':
        title = '正在下载更新…';
        detail = update ? `新版本 v${update.version}` : '正在下载安装包';
        break;
      case 'installing':
        title = '正在安装更新…';
        detail = '下载完成，安装后应用将自动重启。';
        break;
      case 'restarting':
        title = '正在重启应用…';
        detail = '更新已安装，应用即将重新启动。';
        break;
      case 'error':
        title = '更新失败';
        detail = '检查或安装更新时出错，请查看下方提示。';
        break;
    }

    updateStatus.dataset.phase = state.phase;
    updateDot.className = 'status-dot';
    updateTitle.textContent = title;
    updateDetail.textContent = detail;

    updateError.hidden = !(state.phase === 'error' && state.error);
    updateError.textContent =
      state.phase === 'error' && state.error ? state.error : '';

    const showProgress =
      (state.phase === 'downloading' || state.phase === 'installing') &&
      state.progress !== null;
    progressBox.hidden = !showProgress;
    if (showProgress && state.progress) {
      const { downloaded, total, percent } = state.progress;
      progressBar.style.width = `${percent ?? 0}%`;
      progressText.textContent =
        total !== null
          ? `${percent !== null ? `${percent.toFixed(0)}% · ` : ''}${formatBytes(downloaded)} / ${formatBytes(total)}`
          : `已下载 ${formatBytes(downloaded)}`;
    } else {
      progressBar.style.width = '0%';
      progressText.textContent = '';
    }

    const body = update?.body?.trim() ?? '';
    notes.hidden = !body;
    notes.textContent = body;

    const phase = state.phase;
    checkButton.disabled =
      phase === 'checking' ||
      phase === 'preparing' ||
      phase === 'downloading' ||
      phase === 'installing' ||
      phase === 'restarting';
    installButton.hidden =
      !update ||
      phase === 'checking' ||
      phase === 'preparing' ||
      phase === 'downloading' ||
      phase === 'installing' ||
      phase === 'restarting';
    installButton.disabled = false;
    installButton.textContent =
      phase === 'blocked' || phase === 'error' ? '重试安装' : '下载并安装';

    updateOperationHint(state, ctx.store.get().operation);
  };

  const setVersion = (version: string): void => {
    aboutVersion.textContent = `v${version}`;
    infoVersion.textContent = `v${version}`;
    if (lastState.phase === 'idle' || lastState.phase === 'up-to-date') {
      render(lastState);
    }
  };

  setVersion(currentVersion());
  render(lastState);
  updater.subscribe(render);
  ctx.store.subscribe((snapshot) => {
    setVersion(snapshot.version);
    updateOperationHint(lastState, snapshot.operation);
  });

  checkButton.addEventListener('click', () => {
    if (!ctx.desktop) {
      ctx.toast('更新检查仅在桌面应用中可用', 'warning');
      return;
    }
    ctx.log('手动检查更新');
    void updater.check({ silent: false }).then(() => {
      const state = updater.getState();
      if (state.phase === 'up-to-date') {
        ctx.toast(`已是最新版本 v${currentVersion()}`, 'success', 2600);
      }
    });
  });

  installButton.addEventListener('click', () => {
    if (!ctx.desktop) {
      ctx.toast('更新安装仅在桌面应用中可用', 'warning');
      return;
    }
    ctx.log('请求安装更新');
    void updater.install().then((success) => {
      if (success) {
        return;
      }
      const state = updater.getState();
      if (state.error) {
        ctx.toast(
          state.error,
          state.phase === 'blocked' ? 'warning' : 'error',
          6000,
        );
      }
    });
  });

  return {
    setActive(): void {
      render(lastState);
    },
    startupCheck(): void {
      if (startupCheckDone || !ctx.desktop || !startupCheckBox.checked) {
        return;
      }
      startupCheckDone = true;
      ctx.log('启动静默检查更新');
      void updater.check({ silent: true });
    },
  };
}
