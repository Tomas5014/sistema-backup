import { afterEach, expect, it, vi } from "vitest";
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { InstallationAlerts } from "../src/components/InstallationAlerts";
import { json, clearCookies } from "./helpers";

function stubFetch(handler: (url: string, init: RequestInit) => Response | Promise<Response>) {
  vi.stubGlobal("fetch", vi.fn((url: string, init: RequestInit = {}) => handler(String(url), init)));
}

const alert = { id: "alert-a", kind: "backup_failed", detail: { status: "partial", result_code: "incomplete" }, created_at: "2026-11-18T12:00:00Z", resolved_at: null, acknowledged_at: null, delivery_state: "sent" };
const summary = { configured: true, paused: false, last_backup: { status: "partial", finished_at: "2026-11-18T12:00:00Z", snapshot_id: null }, last_protected_copy: null, next_deadline: "2026-11-19T12:30:00Z", alerts: [alert] };
afterEach(() => { vi.unstubAllGlobals(); clearCookies(); });

it("separa backup, cópia e prazo; reconhece com CSRF sem apagar histórico", async () => {
  document.cookie = "macip_csrf=alerts-csrf; path=/";
  let ack = false;
  stubFetch((url, init) => {
    if (url.endsWith("/ack")) { expect(new Headers(init.headers).get("X-CSRF-Token")).toBe("alerts-csrf"); ack = true; return json({ acknowledged: true }); }
    return json({ ...summary, alerts: [{ ...alert, acknowledged_at: ack ? "2026-11-18T12:01:00Z" : null }] });
  });
  render(<InstallationAlerts id="a" admin />);
  expect(await screen.findByText("Backup falhou")).toBeInTheDocument();
  expect(screen.getByText("Último backup")).toBeInTheDocument();
  expect(screen.getByText("Última cópia protegida")).toBeInTheDocument();
  expect(screen.getByText("Próximo prazo")).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "Reconhecer recebimento" }));
  expect(await screen.findByText(/Reconhecido em/)).toBeInTheDocument();
  expect(screen.getByText("Backup incompleto")).toBeInTheDocument();
});
it("viewer consulta sem solicitar; erro e texto não viram HTML", async () => {
  stubFetch(() => json({ ...summary, alerts: [{ ...alert, kind: "copy_late", detail: { snapshot_a: "<script>sentinel</script>" }, resolved_at: "2026-11-18T13:00:00Z" }] }));
  render(<InstallationAlerts id="b" admin={false} />);
  expect(await screen.findByText("Cópia protegida atrasada")).toBeInTheDocument();
  expect(screen.queryByRole("button")).not.toBeInTheDocument();
  expect(screen.getByText("<script>sentinel</script>")).toBeInTheDocument();
  expect(document.querySelector("script")).toBeNull();
});
it("descarta resposta anterior ao trocar instalação", async () => {
  let resolveA!: (r: Response) => void;
  stubFetch(url => url.includes("/a/") ? new Promise<Response>(resolve => { resolveA = resolve; }) : json({ ...summary, alerts: [{ ...alert, kind: "backup_missing" }] }));
  const page = render(<InstallationAlerts id="a" admin />);
  page.rerender(<InstallationAlerts id="b" admin />);
  expect(await screen.findByText("Backup não confirmado no prazo")).toBeInTheDocument();
  await act(async () => resolveA(json(summary)));
  expect(screen.queryByText("Backup falhou")).not.toBeInTheDocument();
});
it("mostra falta de vínculo e sessão expirada", async () => {
  stubFetch(() => json({ ...summary, configured: false, alerts: [] }));
  const page = render(<InstallationAlerts id="a" admin />);
  expect(await screen.findByText("Monitoramento não configurado")).toBeInTheDocument();
  stubFetch(() => json({ detail: "sessão expirada" }, 401));
  page.rerender(<InstallationAlerts id="b" admin />);
  await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent(/sessão expirada/));
});
