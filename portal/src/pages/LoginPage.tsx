/** Login: CSRF antes do POST; mensagens distintas por tipo de falha. */

import { useState, type FormEvent } from "react";
import { Navigate, useLocation } from "react-router-dom";
import { ApiError } from "../api";
import { useAuth } from "../auth";

export function LoginPage() {
  const { state, signIn } = useAuth();
  const location = useLocation();
  const [loginName, setLoginName] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  if (state === "authenticated") {
    const from = (location.state as { from?: string } | null)?.from ?? "/";
    return <Navigate to={from} replace />;
  }

  async function onSubmit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await signIn(loginName, password);
    } catch (err) {
      if (err instanceof ApiError) {
        // 401: credenciais inválidas (mensagem genérica da API);
        // network: indisponibilidade NÃO é senha errada
        setError(
          err.kind === "unauthenticated"
            ? "Login ou senha inválidos."
            : err.message,
        );
      } else {
        setError("Falha inesperada ao entrar.");
      }
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="login-page">
      <form className="card login-card" onSubmit={onSubmit}>
        <h1>MAC IP Backup</h1>
        <p className="muted">Portal operacional — equipe interna</p>

        <label htmlFor="login">Login</label>
        <input
          id="login"
          name="login"
          autoComplete="username"
          required
          value={loginName}
          onChange={(e) => setLoginName(e.target.value)}
        />

        <label htmlFor="password">Senha</label>
        <input
          id="password"
          name="password"
          type="password"
          autoComplete="current-password"
          required
          value={password}
          onChange={(e) => setPassword(e.target.value)}
        />

        {error ? (
          <p className="error-state" role="alert">
            {error}
          </p>
        ) : null}
        {state === "unavailable" ? (
          <p className="error-state" role="alert">
            API temporariamente indisponível — a sessão não pôde ser
            verificada.
          </p>
        ) : null}

        <button type="submit" className="btn btn-primary" disabled={busy}>
          {busy ? "Entrando…" : "Entrar"}
        </button>
      </form>
    </main>
  );
}
