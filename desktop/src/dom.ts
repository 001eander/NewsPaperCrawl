export function byId<T extends HTMLElement>(id: string): T {
  const element = document.getElementById(id);
  if (!element) {
    throw new Error(`缺少页面元素 #${id}`);
  }
  return element as T;
}

export function createElement<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  className?: string,
  text?: string,
): HTMLElementTagNameMap[K] {
  const element = document.createElement(tag);
  if (className) {
    element.className = className;
  }
  if (text !== undefined) {
    element.textContent = text;
  }
  return element;
}

export function errorMessage(error: unknown): string {
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
  if (error === undefined || error === null || error === '') {
    return '未知错误';
  }
  try {
    return JSON.stringify(error);
  } catch {
    return String(error);
  }
}

export function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes < 0) {
    return '—';
  }
  if (bytes < 1024) {
    return `${bytes} B`;
  }
  const units = ['KB', 'MB', 'GB'];
  let value = bytes / 1024;
  let unit = units[0];
  for (let index = 0; index < units.length; index += 1) {
    unit = units[index];
    if (value < 1024 || index === units.length - 1) {
      break;
    }
    value /= 1024;
  }
  return `${value.toFixed(value >= 100 ? 0 : 1)} ${unit}`;
}

export function formatClock(date: Date): string {
  return date.toLocaleTimeString('zh-CN', { hour12: false });
}

export function setHidden(element: HTMLElement, hidden: boolean): void {
  element.hidden = hidden;
}

export function clearNode(node: Node): void {
  while (node.firstChild) {
    node.removeChild(node.firstChild);
  }
}

export function setBusy(button: HTMLButtonElement, busy: boolean, busyText?: string): void {
  if (!button.dataset.idleLabel) {
    button.dataset.idleLabel = button.textContent ?? '';
  }
  button.disabled = busy;
  if (busy) {
    button.textContent = busyText ?? '处理中…';
  } else {
    button.textContent = button.dataset.idleLabel;
  }
}
