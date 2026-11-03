import { useQuery } from "@tanstack/react-query";
import { allGroups, api, type Provider } from "./api/client";
export function useGroups() {
  return useQuery({
    queryKey: ["groups"],
    queryFn: ({ signal }) => allGroups(signal),
  });
}
export function useProviders() {
  return useQuery({
    queryKey: ["providers"],
    queryFn: ({ signal }) => api<Provider[]>("/auth/sso/providers", { signal }),
  });
}
