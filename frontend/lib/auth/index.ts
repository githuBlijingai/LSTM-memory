export type UserRole = "viewer" | "operator" | "admin";

export interface AuthUser {
  id: string;
  name: string;
  roles: UserRole[];
}

export interface ServerSession {
  accessToken: string;
  mode: "dev" | "oidc";
  user: AuthUser;
}

export interface SessionView {
  user: AuthUser;
  mode: "dev" | "oidc";
}

const ROLE_RANK: Record<UserRole, number> = {
  viewer: 1,
  operator: 2,
  admin: 3,
};

const VALID_ROLES: UserRole[] = ["viewer", "operator", "admin"];

function developmentRoles(): UserRole[] {
  const configured = (process.env.DEV_AUTH_ROLES ?? "viewer")
    .split(",")
    .map((role) => role.trim())
    .filter((role): role is UserRole => VALID_ROLES.includes(role as UserRole));
  return configured.length > 0 ? configured : ["viewer"];
}

function developmentUser(): AuthUser {
  return {
    id: process.env.DEV_AUTH_SUBJECT ?? "dev-user",
    name: "开发用户",
    roles: developmentRoles(),
  };
}

export function isDevAuthEnabled() {
  return process.env.NODE_ENV !== "production" && process.env.DEV_AUTH_ENABLED === "true";
}

export function hasRole(roles: readonly string[], required: UserRole) {
  return roles.some(
    (role) => ROLE_RANK[role as UserRole] !== undefined && ROLE_RANK[role as UserRole] >= ROLE_RANK[required],
  );
}

/**
 * AG-05 将在此接入 OIDC 服务端会话。Dev Auth 必须显式开启；
 * 后端 `Dev <subject>` 方案不要求令牌，DEV_AUTH_TOKEN 仅为兼容保留。
 * 生产环境不会返回占位会话。
 */
export async function getServerSession(): Promise<ServerSession | null> {
  if (!isDevAuthEnabled()) {
    return null;
  }

  return {
    accessToken: process.env.DEV_AUTH_TOKEN ?? "",
    mode: "dev",
    user: developmentUser(),
  };
}

export async function getSessionView(): Promise<SessionView | null> {
  const session = await getServerSession();
  return session ? { user: session.user, mode: session.mode } : null;
}
