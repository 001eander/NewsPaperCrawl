import type { CoreApi } from './api';
import type { AppSnapshot, Store } from './store';
import type { DialogOptions, DialogResult } from './ui/dialog';
import type { ToastTone } from './ui/toast';

export interface AppContext {
  core: CoreApi;
  store: Store<AppSnapshot>;
  desktop: boolean;
  log(message: string): void;
  toast(message: string, tone?: ToastTone, duration?: number): void;
  dialog(options: DialogOptions): Promise<DialogResult>;
  refreshState(): Promise<void>;
  operationLabel(operation: string | null, stage?: string | null): string;
}
