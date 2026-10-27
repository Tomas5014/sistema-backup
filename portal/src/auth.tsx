/**
 * Contexto de sessão: restauração na abertura, estados distintos
 * (carregando / autenticado / anônimo / API indisponível) e logout
 * com revogação no servidor — falha visível, sem fingir sucesso.
 */

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useState,
  type ReactNode,
} from "react";
import {
  ApiError,
  login as apiLogin,
  logout as apiLogout,
  me as apiMe,
  type Operator,
} from "./api";

export type AuthState = "loading" | "authenticated" | "anonymous" | "unavailable";

interface AuthContextValue {
  state: AuthState;
  operator: Operator | null;
  refresh: () => Promise<void>;
  signIn: (login: string, password: string) => Promise<void>;
  signOut: () => Promise<string | null>; // devolve erro visível ou null
}

const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<AuthState>("loading");
  const [operator, setOperator] = useState<Operator | null>(null);

  const refresh = useCallback(async () => {
    try {
      setOperator(await apiMe());
      setState("authenticated");
    } catch (error) {
      setOperator(null);
      setState(
        error instanceof ApiError && error.kind === "unauthenticated"
          ? "anonymous" // sessão ausente/expirada — não é indisponibilidade
          : "unavailable", // rede/5xx — não confundir com senha errada
      );
    }
  }, []);

  useEffect(() => {
    void refresh(); // restauração da sessão ao abrir a página
  }, [refresh]);

  const signIn = useCallback(
    async (loginName: string, password: string) => {
      setOperator(await apiLogin(loginName, password)); // lança ApiError
      setState("authenticated");
    },
    [],
  );

  const signOut = useCallback(async () => {
    try {
      await apiLogout(); // revoga no servidor — cookie limpo pela resposta
      setOperator(null);
      setState("anonymous");
      return null;
    } catch (error) {
      return error instanceof ApiError
        ? error.message
        : "Falha desconhecida ao encerrar a sessão.";
    }
  }, []);

  return (
    <AuthContext.Provider value={{ state, operator, refresh, signIn, signOut }}>
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth fora de AuthProvider");
  return ctx;
}
