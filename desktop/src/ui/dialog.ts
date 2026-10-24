import { byId } from '../dom';

export type DialogTone = 'info' | 'warning' | 'danger' | 'success';

export interface DialogOptions {
  title: string;
  message: string;
  tone?: DialogTone;
  confirmLabel?: string;
  /** 传 null 隐藏取消按钮 */
  cancelLabel?: string | null;
}

export type DialogResult = 'confirm' | 'cancel';

let activeFinish: ((result: DialogResult) => void) | null = null;

export function dialog(options: DialogOptions): Promise<DialogResult> {
  const element = byId<HTMLDialogElement>('app-dialog');
  const title = byId<HTMLHeadingElement>('app-dialog-title');
  const message = byId<HTMLDivElement>('app-dialog-message');
  const confirm = byId<HTMLButtonElement>('app-dialog-confirm');
  const cancel = byId<HTMLButtonElement>('app-dialog-cancel');

  activeFinish?.('cancel');

  title.textContent = options.title;
  message.textContent = options.message;
  element.dataset.tone = options.tone ?? 'info';
  confirm.textContent = options.confirmLabel ?? '确定';
  cancel.textContent = options.cancelLabel ?? '取消';
  cancel.hidden = options.cancelLabel === null;

  return new Promise<DialogResult>((resolve) => {
    let settled = false;

    const cleanup = (): void => {
      confirm.removeEventListener('click', onConfirm);
      cancel.removeEventListener('click', onCancel);
      element.removeEventListener('cancel', onNativeCancel);
      element.removeEventListener('click', onBackdrop);
      element.removeEventListener('close', onClose);
      if (activeFinish === finish) {
        activeFinish = null;
      }
    };

    const finish = (result: DialogResult): void => {
      if (settled) {
        return;
      }
      settled = true;
      cleanup();
      if (element.open) {
        element.close();
      }
      resolve(result);
    };

    const onConfirm = (): void => finish('confirm');
    const onCancel = (): void => finish('cancel');
    const onClose = (): void => finish('cancel');
    const onNativeCancel = (event: Event): void => {
      event.preventDefault();
      finish('cancel');
    };
    const onBackdrop = (event: MouseEvent): void => {
      if (event.target === element) {
        finish('cancel');
      }
    };

    activeFinish = finish;
    confirm.addEventListener('click', onConfirm);
    cancel.addEventListener('click', onCancel);
    element.addEventListener('cancel', onNativeCancel);
    element.addEventListener('click', onBackdrop);
    element.addEventListener('close', onClose);

    if (!element.open) {
      element.showModal();
    }
    confirm.focus();
  });
}

export function closeDialogs(): void {
  activeFinish?.('cancel');
  for (const element of document.querySelectorAll<HTMLDialogElement>('dialog[open]')) {
    element.close();
  }
}
