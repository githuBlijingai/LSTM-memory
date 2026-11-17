export type ApiRequestOptions<TBody = unknown> = Omit<RequestInit, "body"> & {
  body?: TBody;
  requestId?: string;
};

export interface ApiErrorOptions {
  status: number;
  code: string;
  requestId?: string;
  details?: unknown;
  cause?: unknown;
}
