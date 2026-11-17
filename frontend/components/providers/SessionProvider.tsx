"use client";

import { createContext, useContext, type ReactNode } from "react";
import { hasRole, type SessionView, type UserRole } from "@/lib/auth";

const SessionContext = createContext<SessionView | null>(null);

export function SessionProvider({
  children,
  session,
}: {
  children: ReactNode;
  session: SessionView | null;
}) {
  return <SessionContext.Provider value={session}>{children}</SessionContext.Provider>;
}

export function useSession() {
  return useContext(SessionContext);
}

export function useHasRole(required: UserRole) {
  const session = useSession();
  return session ? hasRole(session.user.roles, required) : false;
}
