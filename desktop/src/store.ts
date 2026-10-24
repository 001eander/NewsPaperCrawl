export type StoreListener<T> = (value: T) => void;

export class Store<T extends object> {
  private value: T;
  private readonly listeners = new Set<StoreListener<T>>();

  constructor(initial: T) {
    this.value = initial;
  }

  get(): Readonly<T> {
    return this.value;
  }

  set(patch: Partial<T>): void {
    this.value = { ...this.value, ...patch };
    for (const listener of this.listeners) {
      listener(this.value);
    }
  }

  subscribe(listener: StoreListener<T>): () => void {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  }
}

export interface AppSnapshot {
  version: string;
  operation: string | null;
  stage: string | null;
  paths: import('./types').Paths | null;
  backendError: string | null;
}

export const appStore = new Store<AppSnapshot>({
  version: __APP_VERSION__,
  operation: null,
  stage: null,
  paths: null,
  backendError: null,
});
