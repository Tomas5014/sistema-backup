/**
 * Contexto de sessão: restauração na abertura, estados distintos
 * (carregando / autenticado / anônimo / API indisponível) e logout
 * com revogação no servidor — falha visível, sem fingir sucesso.
 *
 * Coordenação por GERAÇÃO: refresh, signIn e signOut incrementam um
 * contador; só a operação mais recente altera a sessão. Um /auth/me
 * antigo não desfaz login nem logout concluídos — sucesso e erro
 * obsoletos são descartados igualmente (e descarte nunca vira
 * "API indisponível"). Desmontar o provider invalida o pendente.
 */

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useRef,
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
  // toda operação nova invalida as respostas das anteriores
  const generation = useRef(0);

  useEffect(() => {
    const alive = () => generation.current;
    void (async () => {
      const gen = ++generation.current;
      try {
        const op = await apiMe(); // restauração ao abrir a página
        if (gen !== alive()) return;
        setOperator(op);
        setState("authenticated");
      } catch (error) {
        if (gen !== alive()) return;
        setOperator(null);
        setState(
          error instanceof ApiError && error.kind === "unauthenticated"
            ? "anonymous"
            : "unavailable",
        );
      }
    })();
    return () => {
      generation.current++; // desmontou → pendente descartado
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const refresh = useCallback(async () => {
    const gen = ++generation.current;
    try {
      const op = await apiMe();
      if (gen !== generation.current) return;
      setOperator(op);
      setState("authenticated");
    } catch (error) {
      if (gen !== generation.current) return;
      setOperator(null);
      setState(
        error instanceof ApiError && error.kind === "unauthenticated"
          ? "anonymous" // sessão ausente/expirada — não é indisponibilidade
          : "unavailable", // rede/5xx — não confundir com senha errada
      );
    }
  }, []);

  const signIn = useCallback(async (loginName: string, password: string) => {
    const gen = ++generation.current;
    const op = await apiLogin(loginName, password); // lança ApiError
    if (gen !== generation.current) return; // algo mais recente assumiu
    setOperator(op);
    setState("authenticated");
  }, []);

  const signOut = useCallback(async () => {
    const gen = ++generation.current;
    try {
      await apiLogout(); // revoga no servidor — cookie limpo pela resposta
      if (gen === generation.current) {
        setOperator(null);
        setState("anonymous");
      }
      return null;
    } catch (error) {
      // a falha pertence a ESTE clique — fica visível mesmo se outra
      // operação assumiu; a sessão pode continuar ativa no servidor
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
