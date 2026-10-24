import { byId, createElement } from '../dom';
import { toast } from '../ui/toast';

const MAX_LINES = 5000;
const TRIM_LINES = 1000;

export interface LogsViewApi {
  append(message: string): void;
  appendLocal(message: string): void;
  setActive(active: boolean): void;
  clear(): void;
}

export function createLogsView(): LogsViewApi {
  const panel = byId<HTMLDivElement>('log-view');
  const meta = byId<HTMLParagraphElement>('log-meta');
  const autoscroll = byId<HTMLInputElement>('log-autoscroll');
  const copyButton = byId<HTMLButtonElement>('log-copy');
  const clearButton = byId<HTMLButtonElement>('log-clear');

  let active = false;

  const updateMeta = (): void => {
    meta.textContent = `${panel.childElementCount} 行 · 上限 ${MAX_LINES} 行`;
  };

  const isNearBottom = (): boolean =>
    panel.scrollHeight - panel.scrollTop - panel.clientHeight < 48;

  const appendLine = (text: string, local: boolean): void => {
    const shouldStick = autoscroll.checked && (isNearBottom() || !active);
    const line = createElement('div', 'log-line');
    if (local) {
      line.classList.add('is-local');
    } else if (/\b(ERROR|CRITICAL)\b/i.test(text) || /错误|异常|失败/.test(text)) {
      line.classList.add('is-error');
    } else if (/\b(WARNING|WARN)\b/i.test(text) || /警告/.test(text)) {
      line.classList.add('is-warn');
    } else if (/\bDEBUG\b/i.test(text)) {
      line.classList.add('is-debug');
    }
    line.textContent = text;
    panel.append(line);

    if (panel.childElementCount > MAX_LINES) {
      for (
        let index = 0;
        index < TRIM_LINES && panel.firstElementChild;
        index += 1
      ) {
        panel.firstElementChild.remove();
      }
    }

    if (shouldStick) {
      panel.scrollTop = panel.scrollHeight;
    }
    updateMeta();
  };

  const append = (message: string): void => {
    const text = message.replace(/\r?\n$/, '');
    if (!text) {
      return;
    }
    for (const line of text.split(/\r?\n/)) {
      appendLine(line, false);
    }
  };

  const appendLocal = (message: string): void => {
    appendLine(`[界面] ${message}`, true);
  };

  const clear = (): void => {
    panel.replaceChildren();
    updateMeta();
    toast('日志已清空', 'info', 2200);
  };

  clearButton.addEventListener('click', clear);

  copyButton.addEventListener('click', () => {
    const text = Array.from(panel.children)
      .map((line) => line.textContent ?? '')
      .join('\n');
    if (!text) {
      toast('暂无可复制的日志', 'warning', 2200);
      return;
    }
    void navigator.clipboard
      .writeText(text)
      .then(() => toast('日志已复制到剪贴板', 'success', 2200))
      .catch((error: unknown) => {
        const message = error instanceof Error ? error.message : String(error);
        toast(`复制失败：${message}`, 'error');
      });
  });

  updateMeta();

  return {
    append,
    appendLocal,
    clear,
    setActive(nextActive: boolean): void {
      active = nextActive;
      if (active && autoscroll.checked) {
        panel.scrollTop = panel.scrollHeight;
      }
    },
  };
}
