import type {
  ApiEnvelope,
  ModelDiffPayload,
  ModelJobPayload,
  ModelPrivateListPayload
} from "./types";

type PrivateModelPath =
  | "/api/models/list"
  | "/api/models/diff"
  | "/api/models/register"
  | "/api/models/approve"
  | "/api/models/reject"
  | "/api/models/rollback";
type PrivateModelRequestPath = PrivateModelPath | `/api/models/${"list" | "diff"}?${string}`;

export type ModelApprovalRequest = {
  model: string;
  version: string;
  reason: string;
  activate?: boolean;
  confirm_production?: boolean;
};

export type ModelRegisterRequest = {
  model: string;
  version: string;
  source_version?: string;
  description?: string;
  scenario?: string;
  run_id?: string | number;
};

async function csrfTokenForAuthenticatedWrite(): Promise<string> {
  const response = await fetch("/api/auth/status", {
    cache: "no-store",
    credentials: "same-origin"
  });
  const payload = await response.json() as Record<string, unknown>;
  if (!response.ok || payload.logged_in !== true) {
    throw new Error("需要先登录后台，当前操作未执行。");
  }
  const token = typeof payload.csrf_token === "string" ? payload.csrf_token : "";
  if (!token) {
    throw new Error("后台会话缺少写操作校验信息，请刷新后台登录后重试。");
  }
  return token;
}

function messageFrom(payload: unknown, fallback: string): string {
  if (!payload || typeof payload !== "object") return fallback;
  const record = payload as Record<string, unknown>;
  if (typeof record.message === "string" && record.message.trim()) return record.message;
  if (typeof record.error === "string" && record.error.trim()) return record.error;
  if (record.error && typeof record.error === "object") {
    const message = (record.error as Record<string, unknown>).message;
    if (typeof message === "string" && message.trim()) return message;
  }
  return fallback;
}

async function privateModelRequest<T>(
  path: PrivateModelRequestPath,
  init: RequestInit = {}
): Promise<T> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 15000);
  try {
    const response = await fetch(path, {
      ...init,
      cache: "no-store",
      credentials: "same-origin",
      headers: {
        "Content-Type": "application/json",
        ...init.headers
      },
      signal: controller.signal
    });
    const text = await response.text();
    let payload: (ApiEnvelope<T> & T) | null = null;
    try {
      payload = text ? JSON.parse(text) as ApiEnvelope<T> & T : {} as ApiEnvelope<T> & T;
    } catch {
      throw new Error("后台接口返回格式异常，请稍后重试。");
    }
    if (!response.ok || payload.ok === false) {
      if (response.status === 401) throw new Error("需要先登录后台，当前操作未执行。");
      throw new Error(messageFrom(payload, "后台模型任务提交失败，请稍后重试。"));
    }
    return (payload && typeof payload === "object" && "data" in payload && payload.data !== undefined
      ? payload.data
      : payload) as T;
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") {
      throw new Error("后台模型接口响应超时，当前操作未执行。");
    }
    throw error;
  } finally {
    clearTimeout(timer);
  }
}

export function getPrivateModelList(model = "signal-decision") {
  const query = new URLSearchParams({ model }).toString();
  return privateModelRequest<ModelPrivateListPayload>(`/api/models/list?${query}`);
}

export function getPrivateModelDiff(model: string, version: string) {
  const query = new URLSearchParams({ model, version }).toString();
  return privateModelRequest<ModelDiffPayload>(`/api/models/diff?${query}`);
}

export function submitModelRegistryJob(
  action: "approve" | "reject" | "rollback",
  request: ModelApprovalRequest
) {
  return csrfTokenForAuthenticatedWrite().then((csrfToken) => (
    privateModelRequest<ModelJobPayload>(`/api/models/${action}`, {
      method: "POST",
      headers: { "X-CSRF-Token": csrfToken },
      body: JSON.stringify(request)
    })
  ));
}

export function registerCandidateModel(request: ModelRegisterRequest) {
  return csrfTokenForAuthenticatedWrite().then((csrfToken) => (
    privateModelRequest<ModelJobPayload>("/api/models/register", {
      method: "POST",
      headers: { "X-CSRF-Token": csrfToken },
      body: JSON.stringify(request)
    })
  ));
}
