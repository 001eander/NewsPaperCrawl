import { open } from '@tauri-apps/plugin-dialog';
import type { AppContext } from '../context';
import { byId, errorMessage } from '../dom';
import type { RunParams, Stage } from '../types';

const STAGES: readonly Stage[] = ['import', 'issues', 'boards', 'articles'];

export interface RunViewApi {
  setOperation(operation: string | null, stage?: string | null): void;
  login(): Promise<void>;
}

class ValidationError extends Error {
  constructor(
    message: string,
    readonly input?: HTMLElement,
  ) {
    super(message);
  }
}

function isStage(value: string | undefined): value is Stage {
  return value !== undefined && (STAGES as readonly string[]).includes(value);
}

export function createRunView(ctx: AppContext): RunViewApi {
  const loginButton = byId<HTMLButtonElement>('login-button');
  const browseButton = byId<HTMLButtonElement>('browse-csv');
  const stopButton = byId<HTMLButtonElement>('stop-button');
  const runButtons = Array.from(
    document.querySelectorAll<HTMLButtonElement>('[data-run-stage]'),
  );
  const runStateText = byId<HTMLElement>('run-state-text');
  const runStateHint = byId<HTMLElement>('run-state-hint');
  const runDot = byId<HTMLElement>('run-dot');

  const importCsv = byId<HTMLInputElement>('import-csv');
  const issuesStartYear = byId<HTMLInputElement>('issues-start-year');
  const issuesEndYear = byId<HTMLInputElement>('issues-end-year');
  const issuesStartMonth = byId<HTMLInputElement>('issues-start-month');
  const issuesEndMonth = byId<HTMLInputElement>('issues-end-month');
  const issuesConcurrency = byId<HTMLInputElement>('issues-concurrency');
  const boardsPaperid = byId<HTMLInputElement>('boards-paperid');
  const boardsLimit = byId<HTMLInputElement>('boards-limit');
  const boardsConcurrency = byId<HTMLInputElement>('boards-concurrency');
  const articlesLimit = byId<HTMLInputElement>('articles-limit');
  const articlesConcurrency = byId<HTMLInputElement>('articles-concurrency');

  const errorNodes = new Map<string, HTMLParagraphElement>();
  for (const node of document.querySelectorAll<HTMLParagraphElement>(
    '[data-error-for]',
  )) {
    const stage = node.dataset.errorFor;
    if (stage) {
      errorNodes.set(stage, node);
    }
  }

  const cardFor = (stage: Stage): HTMLElement | null =>
    document.querySelector<HTMLElement>(`[data-stage="${stage}"]`);

  const clearStageError = (stage: Stage): void => {
    const node = errorNodes.get(stage);
    if (node) {
      node.hidden = true;
      node.textContent = '';
    }
    for (const input of cardFor(stage)?.querySelectorAll<HTMLInputElement>('input') ??
      []) {
      input.removeAttribute('aria-invalid');
    }
  };

  const showValidation = (stage: Stage, error: ValidationError): void => {
    clearStageError(stage);
    const node = errorNodes.get(stage);
    if (node) {
      node.textContent = error.message;
      node.hidden = false;
    }
    if (error.input) {
      error.input.setAttribute('aria-invalid', 'true');
      error.input.focus();
      error.input.scrollIntoView({ block: 'center', behavior: 'smooth' });
    }
  };

  const optionalInt = (
    input: HTMLInputElement,
    label: string,
    min = 0,
  ): number | undefined => {
    const raw = input.value.trim();
    if (!raw) {
      return undefined;
    }
    if (!/^\d+$/.test(raw)) {
      throw new ValidationError(`${label} 必须是非负整数`, input);
    }
    const value = Number(raw);
    if (!Number.isFinite(value) || value < min) {
      throw new ValidationError(`${label} 不能小于 ${min}`, input);
    }
    return value;
  };

  const requiredInt = (
    input: HTMLInputElement,
    label: string,
    min: number,
    max: number,
  ): number => {
    const raw = input.value.trim();
    if (!raw) {
      throw new ValidationError(`${label} 为必填项`, input);
    }
    if (!/^\d+$/.test(raw)) {
      throw new ValidationError(`${label} 必须是整数`, input);
    }
    const value = Number(raw);
    if (value < min || value > max) {
      throw new ValidationError(`${label} 需在 ${min}–${max} 之间`, input);
    }
    return value;
  };

  const collect = (stage: Stage): RunParams => {
    if (stage === 'import') {
      const csv = importCsv.value.trim();
      return csv ? { csv } : {};
    }

    if (stage === 'issues') {
      const start_year = requiredInt(issuesStartYear, '起始年', 1900, 2100);
      const end_year = requiredInt(issuesEndYear, '结束年', 1900, 2100);
      if (end_year < start_year) {
        throw new ValidationError('结束年不能早于起始年', issuesEndYear);
      }
      const start_month = optionalInt(issuesStartMonth, '开始月份', 1);
      const end_month = optionalInt(issuesEndMonth, '结束月份', 1);
      if (start_month !== undefined && start_month > 12) {
        throw new ValidationError('开始月份需在 1–12 之间', issuesStartMonth);
      }
      if (end_month !== undefined && end_month > 12) {
        throw new ValidationError('结束月份需在 1–12 之间', issuesEndMonth);
      }
      if (
        start_year === end_year &&
        start_month !== undefined &&
        end_month !== undefined &&
        start_month > end_month
      ) {
        throw new ValidationError('开始月份不能晚于结束月份', issuesStartMonth);
      }
      const params: RunParams = { start_year, end_year };
      if (start_month !== undefined) {
        params.start_month = start_month;
      }
      if (end_month !== undefined) {
        params.end_month = end_month;
      }
      const concurrency = optionalInt(issuesConcurrency, '并发', 1);
      if (concurrency !== undefined) {
        params.concurrency = concurrency;
      }
      return params;
    }

    if (stage === 'boards') {
      const params: RunParams = {};
      const paperid = boardsPaperid.value.trim();
      if (paperid) {
        params.paperid = paperid;
      }
      const limit = optionalInt(boardsLimit, 'limit', 1);
      if (limit !== undefined) {
        params.limit = limit;
      }
      const concurrency = optionalInt(boardsConcurrency, '并发', 1);
      if (concurrency !== undefined) {
        params.concurrency = concurrency;
      }
      return params;
    }

    const params: RunParams = {};
    const limit = optionalInt(articlesLimit, 'limit', 1);
    if (limit !== undefined) {
      params.limit = limit;
    }
    const concurrency = optionalInt(articlesConcurrency, '并发', 1);
    if (concurrency !== undefined) {
      params.concurrency = concurrency;
    }
    return params;
  };

  const setOperation = (
    operation: string | null,
    stage: string | null = null,
  ): void => {
    const busy = operation !== null;
    for (const button of runButtons) {
      button.disabled = busy;
    }
    loginButton.disabled = busy;
    browseButton.disabled = busy;
    stopButton.disabled = !busy;
    stopButton.textContent = '停止任务';
    runDot.classList.toggle('is-busy', busy);
    runStateText.textContent = busy
      ? operation === 'login'
        ? '正在登录…'
        : `正在运行 ${ctx.operationLabel(operation, stage)}…`
      : '空闲';
    runStateHint.textContent = busy
      ? '可以点击右侧停止；当前请求会先安全收尾。'
      : '选择阶段并填写参数后开始运行。';
  };

  const start = async (stage: Stage): Promise<void> => {
    if (ctx.store.get().operation !== null) {
      ctx.toast('已有任务在运行，请先停止或等待完成', 'warning');
      return;
    }

    let params: RunParams;
    try {
      params = collect(stage);
    } catch (error) {
      if (error instanceof ValidationError) {
        showValidation(stage, error);
        ctx.toast(error.message, 'error');
      } else {
        ctx.toast(`参数校验失败：${errorMessage(error)}`, 'error');
      }
      return;
    }

    clearStageError(stage);
    ctx.store.set({ operation: 'run', stage });
    ctx.log(`提交阶段 ${stage}，参数 ${JSON.stringify(params)}`);

    try {
      await ctx.core.run(stage, params);
      await ctx.refreshState();
    } catch (error) {
      const message = errorMessage(error);
      ctx.store.set({ operation: null, stage: null });
      ctx.log(`阶段 ${stage} 启动失败：${message}`);
      await ctx.dialog({
        title: '启动失败',
        message,
        tone: 'danger',
      });
    }
  };

  const login = async (): Promise<void> => {
    if (ctx.store.get().operation !== null) {
      ctx.toast('当前有任务在运行，请先停止后再登录', 'warning');
      return;
    }
    ctx.store.set({ operation: 'login', stage: null });
    ctx.log('启动登录流程');
    try {
      await ctx.core.login();
      await ctx.refreshState();
    } catch (error) {
      const message = errorMessage(error);
      ctx.store.set({ operation: null, stage: null });
      ctx.log(`登录启动失败：${message}`);
      await ctx.dialog({
        title: '登录启动失败',
        message,
        tone: 'danger',
      });
    }
  };

  for (const button of runButtons) {
    button.addEventListener('click', () => {
      const stage = button.dataset.runStage;
      if (isStage(stage)) {
        void start(stage);
      }
    });
  }

  loginButton.addEventListener('click', () => {
    void login();
  });

  browseButton.addEventListener('click', () => {
    void (async () => {
      if (!ctx.desktop) {
        ctx.toast('系统文件选择器仅在桌面应用中可用', 'warning');
        return;
      }
      try {
        const selected = await open({
          title: '选择报纸清单 CSV',
          multiple: false,
          directory: false,
          filters: [{ name: 'CSV 文件', extensions: ['csv'] }],
        });
        if (typeof selected === 'string' && selected) {
          importCsv.value = selected;
          clearStageError('import');
        }
      } catch (error) {
        ctx.toast(`打开文件选择器失败：${errorMessage(error)}`, 'error');
      }
    })();
  });

  stopButton.addEventListener('click', () => {
    if (ctx.store.get().operation === null) {
      return;
    }
    stopButton.disabled = true;
    stopButton.textContent = '正在停止…';
    void ctx.core
      .cancel()
      .then(() => {
        ctx.log('已请求停止当前任务');
        ctx.toast('已请求停止，等待当前请求收尾', 'info');
      })
      .catch((error: unknown) => {
        stopButton.disabled = false;
        stopButton.textContent = '停止任务';
        ctx.toast(`停止失败：${errorMessage(error)}`, 'error');
      });
  });

  setOperation(ctx.store.get().operation, ctx.store.get().stage);
  ctx.store.subscribe((snapshot) => {
    setOperation(snapshot.operation, snapshot.stage);
  });

  return { setOperation, login };
}
