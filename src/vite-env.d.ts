/// <reference types="vite/client" />

// ES2022 Error `cause` support (tsconfig targets ES2020, so lib.d.ts lacks it)
interface ErrorOptions {
  cause?: unknown;
}

interface ErrorConstructor {
  new (message?: string, options?: ErrorOptions): Error;
  (message?: string, options?: ErrorOptions): Error;
}

