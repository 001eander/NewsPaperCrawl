import { byId, createElement } from '../dom';

export type ToastTone = 'info' | 'success' | 'warning' | 'error';

const MARKS: Record<ToastTone, string> = {
  info: 'i',
  success: '✓',
  warning: '!',
  error: '!',
};

const MAX_TOASTS = 4;

export function toast(
  message: string,
  tone: ToastTone = 'info',
  duration = 4200,
): void {
  const container = byId<HTMLDivElement>('toasts');
  const item = createElement('div', `toast is-${tone}`);
  item.setAttribute('role', tone === 'error' ? 'alert' : 'status');

  const mark = createElement('span', 'toast-mark', MARKS[tone]);
  const text = createElement('span', 'toast-message', message);
  const close = createElement('button', 'toast-close', '×');
  close.type = 'button';
  close.setAttribute('aria-label', '关闭提示');

  let removed = false;
  const remove = (): void => {
    if (removed) {
      return;
    }
    removed = true;
    item.classList.add('is-leaving');
    window.setTimeout(() => item.remove(), 180);
  };

  let timer = 0;

  close.addEventListener('click', remove);
  item.addEventListener('mouseenter', () => {
    window.clearTimeout(timer);
  });
  item.addEventListener('mouseleave', () => {
    timer = window.setTimeout(remove, Math.max(1600, duration / 2));
  });

  item.append(mark, text, close);
  container.append(item);

  while (container.children.length > MAX_TOASTS) {
    container.firstElementChild?.remove();
  }

  timer = window.setTimeout(remove, duration);
}
