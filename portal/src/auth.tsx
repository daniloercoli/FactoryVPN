import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useRef,
  type ReactNode,
} from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  api,
  ApiError,
  setCsrf,
  invalidateRequests,
  type Session,
  type CurrentUser,
} from "./api/client";

type Auth = {
  session: Session | null;
  user: CurrentUser | null;
  loading: boolean;
  error: unknown;
  accept: (session: Session) => Promise<void>;
  clear: () => Promise<void>;
  can: (permission: string) => boolean;
};
const AuthContext = createContext<Auth | null>(null);

function accessKey(session: Session | null | undefined) {
  if (!session) return "anonymous";
  const { user } = session;
  return JSON.stringify([
    user.id,
    user.auth_mode,
    user.is_admin,
    [...user.permissions].sort(),
    [...user.group_ids].sort(),
  ]);
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const client = useQueryClient();
  const channel = useRef<BroadcastChannel | null>(null);
  const query = useQuery<Session | null>({
    queryKey: ["session"],
    queryFn: async ({ signal }) => {
      try {
        const session = await api<Session>("/web/session", { signal });
        const previous = client.getQueryData<Session | null>(["session"]);
        if (
          previous &&
          (previous.csrf_token !== session.csrf_token ||
            accessKey(previous) !== accessKey(session))
        ) {
          // An SSO callback or another window can change the cookie without a
          // broadcast. Discard the previous account's data before exposing the
          // new session, including late responses from outstanding mutations.
          invalidateRequests();
          await client.cancelQueries({
            predicate: (q) => q.queryKey[0] !== "session",
          });
          client.removeQueries({
            predicate: (q) => q.queryKey[0] !== "session",
          });
        }
        return session;
      } catch (error) {
        if (error instanceof ApiError && error.status === 401) return null;
        throw error;
      }
    },
    refetchInterval: 30000,
    refetchOnWindowFocus: true,
    retry: false,
  });
  const reset = useCallback(async () => {
    invalidateRequests();
    setCsrf("");
    await client.cancelQueries();
    client.removeQueries({ predicate: (q) => q.queryKey[0] !== "session" });
    client.setQueryData(["session"], null);
  }, [client]);
  const clear = useCallback(async () => {
    await reset();
    channel.current?.postMessage("changed");
  }, [reset]);
  const accept = useCallback(
    async (session: Session) => {
      await reset();
      setCsrf(session.csrf_token);
      client.setQueryData(["session"], session);
      channel.current?.postMessage("changed");
    },
    [client, reset],
  );
  useEffect(() => {
    setCsrf(query.data?.csrf_token || "");
  }, [query.data]);
  useEffect(() => {
    const invalidate = () => {
      void reset();
    };
    window.addEventListener("factoryvpn:unauthorized", invalidate);
    if ("BroadcastChannel" in window) {
      const bc = new BroadcastChannel("factoryvpn-portal");
      channel.current = bc;
      bc.onmessage = () => {
        void reset().then(() =>
          client.invalidateQueries({ queryKey: ["session"] }),
        );
      };
    }
    return () => {
      window.removeEventListener("factoryvpn:unauthorized", invalidate);
      channel.current?.close();
    };
  }, [client, reset]);
  const session = query.data || null;
  const user = session?.user || null;
  return (
    <AuthContext.Provider
      key={accessKey(session)}
      value={{
        session,
        user,
        loading: query.isPending,
        error: query.error,
        accept,
        clear,
        can: (p) => !!user && (user.is_admin || user.permissions.includes(p)),
      }}
    >
      {children}
    </AuthContext.Provider>
  );
}
export function useAuth() {
  const value = useContext(AuthContext);
  if (!value) throw new Error("AuthProvider missing");
  return value;
}
