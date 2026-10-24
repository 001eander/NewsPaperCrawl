import { getCurrentWindow } from '@tauri-apps/api/window';
import { exit } from '@tauri-apps/plugin-process';
import './styles.css';

import { api, isDesktop, listenCoreEvents } from './api';
import type { AppContext } from './context';
import { byId, errorMessage } from './dom';
import { operationLabel } from './labels';
import { appStore } from './store';
import type { CoreState, PathKind } from './types';
import { dialog } from './ui/dialog';
import { toast } from './ui/toast';
import { createAboutView } from './views/about';
import { createLogsView } from './views/logs';
import { createRunView } from './views/run';
import { createSettingsView, loadPaths } from './views/settings';
import { createStatusView } from './views/status';
import { createUpdateController } from './updater';
import { checkForDesktopUpdate, relaunchDesktop } from './updater-tauri';

type Page = 'run' | 'status' | 'logs' | 'settings' | 'about';

const PAGE_META: Record<Page, { title: string; subtitle: string }> = {
  run: { title: '运行', subtitle: '登录并启动四阶段爬取任务' },
  status: { title: '状态', subtitle: '阶段进度、按报纸统计与最近失败' },
  logs: { title: '日志', subtitle: '后端运行日志实时回显' },
  settings: { title: '配置', subtitle: '登录凭据、Bark 推送与本地目录' },
  about: { title: '关于 / 更新', subtitle: '版本信息与桌面端更新' },
};

const PAGES = new Set<Page>(['run', 'status', 'logs', 'settings', 'about']);
const PATH_KINDS: readonly PathKind[] = ['data', 'logs', 'secrets'];

function isPathKind(value: string | undefined): value is PathKind {
  return value !== undefined && (PATH_KINDS as readonly string[]).includes(value);
}

function parsePage(hash: string): Page {
  const key = hash.replace(/^#/, '') as Page;
  return PAGES.has(key) ? key : 'run';
}

function boot(): void {
  const logsView = createLogsView();
  let backendErrorLogged = false;
  let stateRequest: Promise<void> | null = null;

  const refreshState = (): Promise<void> => {
    if (stateRequest) {
      return stateRequest;
    }
    stateRequest = (async () => {
      try {
        const state = (await api.state()) as CoreState | null;
        const version =
          typeof state?.version === 'string' && state.version
            ? state.version
            : __APP_VERSION__;
        const operation =
          typeof state?.operation === 'string' && state.operation
            ? state.operation
            : null;
        const stage =
          typeof state?.stage === 'string' && state.stage ? state.stage : null;
        appStore.set({
          version,
          operation,
          stage,
          backendError: null,
        });
        backendErrorLogged = false;
      } catch (error) {
        const message = errorMessage(error);
        appStore.set({ backendError: message });
        if (!backendErrorLogged) {
          backendErrorLogged = true;
          logsView.appendLocal(`无法连接后端：${message}`);
        }
      }
    })().finally(() => {
      stateRequest = null;
    });
    return stateRequest;
  };

  const updater = createUpdateController({
    check: checkForDesktopUpdate,
    prepareUpdate: async () => {
      await api.prepareUpdate();
    },
    relaunch: relaunchDesktop,
    getOperation: async () => {
      const state = (await api.state()) as CoreState | null;
      if (!state?.operation) {
        return null;
      }
      return operationLabel(state.operation, state.stage ?? null);
    },
    log: (level, message) => {
      logsView.appendLocal(message);
      if (level === 'error') {
        console.error(message);
      }
    },
  });

  const ctx: AppContext = {
    core: api,
    store: appStore,
    desktop: isDesktop,
    log: (message) => logsView.appendLocal(message),
    toast,
    dialog,
    refreshState,
    operationLabel,
  };

  const versionPill = byId<HTMLElement>('version-pill');
  const topbarOperation = byId<HTMLElement>('topbar-operation');
  const topbarOperationText = byId<HTMLElement>('topbar-operation-text');
  const sidebarDot = byId<HTMLElement>('sidebar-dot');
  const sidebarLabel = byId<HTMLElement>('sidebar-operation-label');
  const sidebarHint = byId<HTMLElement>('sidebar-operation-hint');
  const pageTitle = byId<HTMLElement>('page-title');
  const pageSubtitle = byId<HTMLElement>('page-subtitle');
  const quitButton = byId<HTMLButtonElement>('quit-button');

  const runView = createRunView(ctx);
  const statusView = createStatusView(ctx);
  const settingsView = createSettingsView(ctx);
  const aboutView = createAboutView(ctx, updater);

  const renderChrome = (): void => {
    const snapshot = appStore.get();
    const busy = snapshot.operation !== null;
    const offline = Boolean(snapshot.backendError) && !busy;
    const label = operationLabel(snapshot.operation, snapshot.stage);

    versionPill.textContent = `v${snapshot.version}`;
    topbarOperation.classList.toggle('is-busy', busy);
    topbarOperation.classList.toggle('is-idle', !busy && !offline);
    topbarOperation.classList.toggle('is-offline', offline);
    topbarOperation.title = snapshot.backendError ?? '';
    topbarOperationText.textContent = busy
      ? snapshot.operation === 'login'
        ? '登录中'
        : `${label}运行中`
      : offline
        ? '后端未连接'
        : '空闲';

    sidebarDot.classList.toggle('is-busy', busy);
    sidebarDot.classList.toggle('is-offline', offline);
    sidebarLabel.textContent = busy
      ? label
      : offline
        ? '后端未连接'
        : '空闲';
    sidebarHint.textContent = busy
      ? snapshot.operation === 'login'
        ? '正在登录，请稍候'
        : '任务运行中，请稍候'
      : offline
        ? '请查看日志了解详情'
        : '等待任务';
  };

  appStore.subscribe(renderChrome);
  renderChrome();

  const showPage = (page: Page): void => {
    for (const button of document.querySelectorAll<HTMLButtonElement>('[data-page]')) {
      const active = button.dataset.page === page;
      button.classList.toggle('is-active', active);
      if (active) {
        button.setAttribute('aria-current', 'page');
      } else {
        button.removeAttribute('aria-current');
      }
    }
    for (const section of document.querySelectorAll<HTMLElement>('[data-page-view]')) {
      section.classList.toggle('is-active', section.dataset.pageView === page);
    }

    pageTitle.textContent = PAGE_META[page].title;
    pageSubtitle.textContent = PAGE_META[page].subtitle;
    document.title = `${PAGE_META[page].title} · 浙图报纸爬虫`;

    statusView.setActive(page === 'status');
    logsView.setActive(page === 'logs');
    if (page === 'about') {
      aboutView.setActive(true);
    }

    if (window.location.hash !== `#${page}`) {
      window.history.replaceState(null, '', `#${page}`);
    }
  };

  for (const button of document.querySelectorAll<HTMLButtonElement>('[data-page]')) {
    button.addEventListener('click', () => {
      const page = button.dataset.page as Page | undefined;
      if (page && PAGES.has(page)) {
        showPage(page);
      }
    });
  }
  window.addEventListener('hashchange', () => {
    showPage(parsePage(window.location.hash));
  });
  showPage(parsePage(window.location.hash));

  const openPath = (kind: PathKind): void => {
    if (!isDesktop) {
      toast('打开本地目录仅在桌面应用中可用', 'warning');
      return;
    }
    void api.openPath(kind).catch((error: unknown) => {
      const message = `打开目录失败：${errorMessage(error)}`;
      ctx.log(message);
      toast(message, 'error');
    });
  };

  for (const button of document.querySelectorAll<HTMLButtonElement>('[data-open-path]')) {
    button.addEventListener('click', () => {
      const kind = button.dataset.openPath;
      if (isPathKind(kind)) {
        openPath(kind);
      }
    });
  }

  for (const button of document.querySelectorAll<HTMLButtonElement>('[data-close-dialog]')) {
    button.addEventListener('click', () => {
      button.closest('dialog')?.close();
    });
  }

  let quitting = false;
  const quit = async (): Promise<void> => {
    if (quitting) {
      return;
    }
    quitting = true;
    const operation = appStore.get().operation;
    const result = await ctx.dialog({
      title: '退出应用',
      message: operation
        ? '当前任务仍在运行，退出将中断本次运行。确定退出？'
        : '确定退出浙图报纸爬虫？',
      tone: operation ? 'warning' : 'info',
      confirmLabel: '退出',
      cancelLabel: '取消',
    });
    if (result !== 'confirm') {
      quitting = false;
      return;
    }

    try {
      await api.shutdown();
    } catch (error) {
      ctx.log(`关闭后端失败：${errorMessage(error)}`);
    }

    try {
      if (!isDesktop) {
        throw new Error('浏览器预览模式无法退出应用');
      }
      await exit(0);
    } catch (error) {
      quitting = false;
      toast(`退出应用失败：${errorMessage(error)}`, 'error');
    }
  };

  quitButton.addEventListener('click', () => {
    void quit();
  });
  if (isDesktop) {
    void getCurrentWindow().onCloseRequested((event) => {
      event.preventDefault();
      void quit();
    });
  }

  const handleOperationDone = async (
    operation: string | null,
    outcome: string,
    message: string,
    stage: string | null,
  ): Promise<void> => {
    const display = operationLabel(operation, stage);

    if (operation === 'login') {
      if (outcome === 'done') {
        toast(message || '登录完成，cookie 已保存', 'success');
      } else if (outcome === 'stopped') {
        toast(message || '登录已取消', 'warning');
      } else {
        await ctx.dialog({
          title: '登录失败',
          message: message || '登录脚本异常退出，失败原因见日志页。',
          tone: 'danger',
        });
      }
      return;
    }

    if (outcome === 'done') {
      toast(`${display}完成`, 'success');
    } else if (outcome === 'stopped') {
      toast(`${display}已停止`, 'warning');
    } else if (outcome === 'busy') {
      await ctx.dialog({
        title: '已有实例在运行',
        message: message || '同一阶段已有进程在运行，请等待其完成。',
        tone: 'warning',
      });
    } else if (outcome === 'auth') {
      const result = await ctx.dialog({
        title: '需要重新登录',
        message: `登录状态失效：${message || 'cookie 已过期'}\n\n请重新登录后再运行阶段。`,
        tone: 'warning',
        confirmLabel: '立即登录',
        cancelLabel: '稍后',
      });
      if (result === 'confirm') {
        await runView.login();
      }
    } else if (outcome === 'error') {
      await ctx.dialog({
        title: `${display}出错`,
        message: message || '阶段异常中断，详情见日志页。',
        tone: 'danger',
      });
    } else if (message) {
      toast(message, 'info');
    }
  };

  const connectEvents = async (): Promise<void> => {
    try {
      await listenCoreEvents((event) => {
        if (!event || typeof event !== 'object') {
          return;
        }
        if (event.type === 'log') {
          if (typeof event.message === 'string') {
            logsView.append(event.message);
          }
          return;
        }
        if (event.type === 'operation_done') {
          const operation =
            typeof event.operation === 'string' ? event.operation : null;
          const outcome =
            typeof event.outcome === 'string' ? event.outcome : 'error';
          const message =
            typeof event.message === 'string' ? event.message : '';
          const stage = appStore.get().stage;
          appStore.set({ operation: null, stage: null });
          void statusView.refreshIfActive();
          window.setTimeout(() => {
            void refreshState();
          }, 120);
          void handleOperationDone(operation, outcome, message, stage);
        }
      });
      logsView.appendLocal('已连接后端事件流');
    } catch (error) {
      const message = `订阅后端事件失败：${errorMessage(error)}`;
      ctx.log(message);
      if (isDesktop) {
        toast(message, 'error');
      }
    }
  };

  void (async () => {
    if (!isDesktop) {
      appStore.set({ backendError: '浏览器预览模式：未检测到 Tauri 后端' });
      logsView.appendLocal(
        '浏览器预览模式：未检测到 Tauri 后端，运行、状态与配置命令不可用。',
      );
      toast('浏览器预览模式：请使用 npm run tauri dev 启动桌面应用', 'warning', 8000);
      return;
    }

    await connectEvents();
    await Promise.allSettled([
      refreshState(),
      loadPaths(ctx),
      settingsView.load(),
    ]);
    window.setTimeout(() => aboutView.startupCheck(), 1200);
  })();
}

boot();
