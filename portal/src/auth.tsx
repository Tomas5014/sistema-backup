/**
 * Contexto de sessão: restauração na abertura, estados distintos
 * (carregando / autenticado / anônimo / API indisponível) e logout
 * com revogação no servidor — falha visível, sem fingir sucesso.
 *
 * Coordenação por GERAÇÃO + TRANSIÇÃO: signIn/signOut são transições
 * explícitas e têm PRIORIDADE sobre revalidação. Um refresh pedido
 * durante uma transição é suprimido (não busca, não invalida); um
 * refresh que já estava em voo fica obsoleto quando a transição
 * começa — seu resultado nunca sobrescreve login/logout, seja qual
 * for a ordem de conclusão. Desmontar o provider invalida o pendente.
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
  SESSION_RECOVERY_EVENT,
  invalidateSessionRequests,
  login as apiLogin,
  logout as apiLogout,
  me as apiMe,
  type Operator,
} from "./api";

export type AuthState = "loading" | "authenticated" | "anonymous" | "unavailable" | "protection_required";

interface AuthContextValue {
  state: AuthState;
  operator: Operator | null;
  refresh: () => Promise<void>;
  recoverSession: () => Promise<void>;
  signIn: (login: string, password: string) => Promise<void>;
  signOut: () => Promise<string | null>; // devolve erro visível ou null
}

const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<AuthState>("loading");
  const [operator, setOperator] = useState<Operator | null>(null);
  // toda operação nova invalida as respostas das anteriores…
  const generation = useRef(0);
  // …mas uma TRANSIÇÃO (login/logout) também suprime novos refresh
  const transitioning = useRef(0);
  const protectionRequired = useRef(false);

  useEffect(() => {
    const recover = (event: Event) => {
      if (transitioning.current > 0) return;
      generation.current++; // respostas da página/transição anterior não valem
      protectionRequired.current = (event as CustomEvent).detail !== "expired";
      setOperator(null);
      setState((event as CustomEvent).detail === "expired" ? "anonymous" : "protection_required");
    };
    window.addEventListener(SESSION_RECOVERY_EVENT, recover);
    return () => window.removeEventListener(SESSION_RECOVERY_EVENT, recover);
  }, []);

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
      invalidateSessionRequests();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const refresh = useCallback(async () => {
    // revalidação não interfere em login/logout em andamento —
    // suprimida aqui, antes mesmo de tocar na rede ou na geração
    if (transitioning.current > 0 || protectionRequired.current) return;
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

  const recoverSession = useCallback(async () => {
    if (transitioning.current > 0) return;
    protectionRequired.current = false;
    invalidateSessionRequests();
    setState("loading"); // desmonta intenções antigas; nenhuma escrita é repetida
    await refresh();
  }, [refresh]);

  const signIn = useCallback(async (loginName: string, password: string) => {
    invalidateSessionRequests();
    const gen = ++generation.current;
    transitioning.current++;
    try {
      const op = await apiLogin(loginName, password); // lança ApiError
      if (gen !== generation.current) return;
      protectionRequired.current = false;
      setOperator(op);
      setState("authenticated");
    } finally {
      transitioning.current--;
    }
  }, []);

  const signOut = useCallback(async () => {
    invalidateSessionRequests();
    const gen = ++generation.current;
    transitioning.current++;
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
    } finally {
      transitioning.current--;
    }
  }, []);

  return (
    <AuthContext.Provider value={{ state, operator, refresh, recoverSession, signIn, signOut }}>
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth fora de AuthProvider");
  return ctx;
}
