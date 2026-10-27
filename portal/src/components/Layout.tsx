/** Área protegida: revalida a sessão a cada navegação e centraliza
 * os estados carregando / sem sessão / API indisponível. */

import { useEffect, useState } from "react";
import {
  Navigate,
  NavLink,
  Outlet,
  useLocation,
} from "react-router-dom";
import { useAuth } from "../auth";
import { Loading } from "./common";

export function Layout() {
  const { state, operator, refresh, signOut } = useAuth();
  const location = useLocation();
  const [logoutError, setLogoutError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  // reabertura/atualização de rota protegida → revalida a sessão
  useEffect(() => {
    void refresh();
  }, [refresh, location.key]);

  if (state === "loading") {
    return (
      <main className="center">
        <Loading />
      </main>
    );
  }
  if (state === "unavailable") {
    return (
      <main className="center">
        <div className="error-state" role="alert">
          <p>Não foi possível alcançar a API — verifique a conexão.</p>
          <button type="button" className="btn" onClick={() => void refresh()}>
            Tentar novamente
          </button>
        </div>
      </main>
    );
  }
  if (state === "anonymous" || !operator) {
    return (
      <Navigate
        to="/login"
        replace
        state={{ from: location.pathname + location.search }}
      />
    );
  }

  async function onLogout() {
    setBusy(true);
    setLogoutError(null);
    const failure = await signOut();
    if (failure) setLogoutError(failure); // revogação falhou — visível
    setBusy(false);
  }

  return (
    <div className="shell">
      <header className="topbar">
        <span className="brand">MAC IP Backup</span>
        <nav aria-label="Principal">
          <NavLink to="/clients">Clientes</NavLink>
          <NavLink to="/executions">Execuções</NavLink>
        </nav>
        <div className="operator">
          <span className="operator-name">
            {operator.name}
            <span className="badge badge-role">{operator.role}</span>
          </span>
          <button
            type="button"
            className="btn"
            onClick={onLogout}
            disabled={busy}
          >
            {busy ? "Saindo…" : "Sair"}
          </button>
        </div>
      </header>
      {logoutError ? (
        <p className="error-state" role="alert">
          {logoutError} — a sessão pode continuar ativa no servidor.
        </p>
      ) : null}
      <Outlet />
    </div>
  );
}
