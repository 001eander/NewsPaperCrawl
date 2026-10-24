import type { AppContext } from '../context';
import { byId, clearNode, createElement, errorMessage, formatClock } from '../dom';
import { stageLabel } from '../labels';
import { normalizeFailed, normalizeStatus } from '../normalize';
import type { FailedRow, OverviewRow, PerPaperRow } from '../types';

const TABLE_ORDER = ['issues', 'boards', 'articles'];
const AUTO_REFRESH_MS = 5000;
const AUTO_REFRESH_KEY = 'newspaper.status.autoRefresh';

export interface StatusViewApi {
  refresh(): Promise<void>;
  refreshIfActive(): Promise<void>;
  setActive(active: boolean): void;
}

function groupFailed(rows: FailedRow[]): Map<string, FailedRow[]> {
  const groups = new Map<string, FailedRow[]>();
  for (const table of TABLE_ORDER) {
    groups.set(table, []);
  }
  for (const row of rows) {
    const key = row.table || '其他';
    const group = groups.get(key);
    if (group) {
      group.push(row);
    } else {
      groups.set(key, [row]);
    }
  }
  return groups;
}

export function createStatusView(ctx: AppContext): StatusViewApi {
  const overviewBody = byId<HTMLTableSectionElement>('overview-body');
  const perPaperBody = byId<HTMLTableSectionElement>('per-paper-body');
  const paperFilter = byId<HTMLInputElement>('paper-filter');
  const refreshButton = byId<HTMLButtonElement>('status-refresh');
  const autoRefresh = byId<HTMLInputElement>('status-auto');
  const updated = byId<HTMLElement>('status-updated');
  const errorBox = byId<HTMLDivElement>('status-error');
  const failedButton = byId<HTMLButtonElement>('failed-button');
  const failedDialog = byId<HTMLDialogElement>('failed-dialog');
  const failedBody = byId<HTMLDivElement>('failed-body');

  const statTotal = byId<HTMLElement>('stat-total');
  const statDone = byId<HTMLElement>('stat-done');
  const statPending = byId<HTMLElement>('stat-pending');
  const statFailed = byId<HTMLElement>('stat-failed');

  let active = false;
  let loading = false;
  let perPaperRows: PerPaperRow[] = [];

  try {
    if (window.localStorage.getItem(AUTO_REFRESH_KEY) === '0') {
      autoRefresh.checked = false;
    }
  } catch {
    // localStorage 不可用时保持默认开启
  }

  autoRefresh.addEventListener('change', () => {
    try {
      window.localStorage.setItem(AUTO_REFRESH_KEY, autoRefresh.checked ? '1' : '0');
    } catch {
      // 忽略持久化失败
    }
  });

  const emptyRow = (columns: number, text: string): HTMLTableRowElement => {
    const row = createElement('tr');
    const cell = createElement('td', 'empty-cell', text);
    cell.colSpan = columns;
    row.append(cell);
    return row;
  };

  const renderStats = (rows: OverviewRow[]): void => {
    const total = rows.reduce((sum, row) => sum + row.total, 0);
    const done = rows.reduce((sum, row) => sum + row.done, 0);
    const pending = rows.reduce((sum, row) => sum + row.pending, 0);
    const failed = rows.reduce((sum, row) => sum + row.failed, 0);
    statTotal.textContent = total.toLocaleString('zh-CN');
    statDone.textContent = done.toLocaleString('zh-CN');
    statPending.textContent = pending.toLocaleString('zh-CN');
    statFailed.textContent = failed.toLocaleString('zh-CN');
  };

  const renderOverview = (rows: OverviewRow[]): void => {
    clearNode(overviewBody);
    renderStats(rows);
    if (rows.length === 0) {
      overviewBody.append(emptyRow(6, '暂无数据：运行 import 并建立数据库后显示进度。'));
      return;
    }

    for (const row of rows) {
      const tr = createElement('tr');
      const stageCell = createElement('td');
      const stageName = createElement('div', 'stage-name');
      const label = createElement('span', undefined, stageLabel(row.stage));
      const code = createElement('code', undefined, row.stage);
      stageName.append(label, code);
      stageCell.append(stageName);
      tr.append(stageCell);

      if (!row.exists) {
        const cell = createElement('td', 'empty-cell', '尚未建表');
        cell.colSpan = 5;
        tr.append(cell);
        overviewBody.append(tr);
        continue;
      }

      for (const value of [row.total, row.done, row.pending, row.failed]) {
        const cell = createElement('td', 'num', value.toLocaleString('zh-CN'));
        tr.append(cell);
      }

      const progressCell = createElement('td');
      const wrap = createElement('div', 'progress-cell');
      const percent = row.total > 0 ? (row.done / row.total) * 100 : 0;
      const progress = createElement('div', 'progress');
      const bar = createElement('span');
      bar.style.width = `${Math.min(100, Math.max(0, percent))}%`;
      progress.append(bar);
      const text = createElement(
        'span',
        'progress-text',
        row.total > 0 ? `${percent.toFixed(1)}%` : '—',
      );
      wrap.append(progress, text);
      progressCell.append(wrap);
      tr.append(progressCell);
      overviewBody.append(tr);
    }
  };

  const renderPerPaper = (): void => {
    const keyword = paperFilter.value.trim().toLowerCase();
    const rows = keyword
      ? perPaperRows.filter(
          (row) =>
            row.paperid.toLowerCase().includes(keyword) ||
            row.name.toLowerCase().includes(keyword),
        )
      : perPaperRows;

    clearNode(perPaperBody);
    if (perPaperRows.length === 0) {
      perPaperBody.append(
        emptyRow(5, '暂无报纸数据：先运行 import 导入报纸清单。'),
      );
      return;
    }
    if (rows.length === 0) {
      perPaperBody.append(emptyRow(5, '没有匹配的报纸。'));
      return;
    }

    for (const row of rows) {
      const tr = createElement('tr');
      const paperid = createElement('td');
      paperid.append(createElement('span', 'paper-id', row.paperid));
      const name = createElement('td', 'paper-name', row.name || '—');
      tr.append(paperid, name);
      for (const value of [row.issues, row.boards, row.articles]) {
        tr.append(createElement('td', 'center', value));
      }
      perPaperBody.append(tr);
    }
  };

  const refresh = async (manual = false): Promise<void> => {
    if (loading) {
      return;
    }
    loading = true;
    refreshButton.disabled = true;
    try {
      const raw = await ctx.core.status();
      const { overview, perPaper } = normalizeStatus(raw);
      renderOverview(overview);
      perPaperRows = perPaper;
      renderPerPaper();
      updated.textContent = `更新于 ${formatClock(new Date())}`;
      errorBox.hidden = true;
      errorBox.textContent = '';
    } catch (error) {
      const message = `状态读取失败：${errorMessage(error)}`;
      errorBox.textContent = message;
      errorBox.hidden = false;
      updated.textContent = '读取失败';
      if (manual) {
        ctx.log(message);
        ctx.toast(message, 'error');
      }
    } finally {
      loading = false;
      refreshButton.disabled = false;
    }
  };

  const showFailed = async (): Promise<void> => {
    clearNode(failedBody);
    failedBody.append(createElement('p', 'failed-empty', '正在加载最近失败…'));
    if (!failedDialog.open) {
      failedDialog.showModal();
    }

    try {
      const raw = await ctx.core.failed(5);
      const rows = normalizeFailed(raw);
      clearNode(failedBody);
      if (rows.length === 0) {
        failedBody.append(createElement('p', 'failed-empty', '没有失败记录。'));
        return;
      }

      const groups = groupFailed(rows);
      for (const [table, group] of groups) {
        if (group.length === 0) {
          continue;
        }
        const section = createElement('section', 'failed-section');
        const heading = createElement('h3');
        heading.append(
          document.createTextNode(`${stageLabel(table)} `),
          createElement('code', undefined, table),
        );
        section.append(heading);

        const box = createElement('div', 'failed-table');
        const tableElement = createElement('table');
        const thead = createElement('thead');
        const headRow = createElement('tr');
        for (const title of ['paperid', '日期', '错误']) {
          headRow.append(createElement('th', undefined, title));
        }
        thead.append(headRow);

        const tbody = createElement('tbody');
        for (const row of group) {
          const tr = createElement('tr');
          const paperid = createElement('td');
          paperid.append(createElement('span', 'paper-id', row.paperid));
          const error = createElement('td', 'failed-error', row.error);
          error.title = row.error;
          tr.append(paperid, createElement('td', undefined, row.date), error);
          tbody.append(tr);
        }

        tableElement.append(thead, tbody);
        box.append(tableElement);
        section.append(box);
        failedBody.append(section);
      }
    } catch (error) {
      clearNode(failedBody);
      failedBody.append(
        createElement(
          'p',
          'failed-empty',
          `读取失败记录失败：${errorMessage(error)}`,
        ),
      );
      ctx.toast('读取最近失败失败', 'error');
    }
  };

  refreshButton.addEventListener('click', () => {
    void refresh(true);
  });

  failedButton.addEventListener('click', () => {
    void showFailed();
  });

  paperFilter.addEventListener('input', renderPerPaper);

  failedDialog.addEventListener('click', (event) => {
    if (event.target === failedDialog) {
      failedDialog.close();
    }
  });

  window.setInterval(() => {
    if (active && autoRefresh.checked && !document.hidden) {
      void refresh(false);
    }
  }, AUTO_REFRESH_MS);

  return {
    refresh: () => refresh(false),
    async refreshIfActive(): Promise<void> {
      if (active) {
        await refresh(false);
      }
    },
    setActive(nextActive: boolean): void {
      const becameActive = nextActive && !active;
      active = nextActive;
      if (becameActive) {
        void refresh(false);
      }
    },
  };
}
