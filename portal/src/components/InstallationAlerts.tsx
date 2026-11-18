import { useEffect, useRef, useState } from "react";
import { ApiError, acknowledgeAlert, installationAlerts, type InstallationAlerts as Summary } from "../api";
import { formatDate } from "../format";

const labels = { backup_failed: "Backup falhou", backup_missing: "Backup não confirmado no prazo", copy_late: "Cópia protegida atrasada" };
const detailLabels: Record<string, string> = { status: "Resultado", result_code: "Motivo", execution_id: "Execução", occurrence: "Ocorrência agendada", deadline: "Prazo", snapshot_a: "Snapshot em A", binding_id: "Vínculo de proteção" };
const resultLabels: Record<string, string> = { success: "sucesso", partial: "parcial", failed: "falha", interrupted: "interrompido", incomplete: "Backup incompleto", restic_failed: "Restic falhou", validation_failed: "Validação local falhou" };
const deliveries: Record<string, string> = { pending: "Envio pendente", sending: "Envio sem confirmação — conferir capturador", uncertain: "Entrega incerta — conferir capturador", sent: "Enviado ao capturador de laboratório" };

export function InstallationAlerts({ id, admin }: { id: string; admin: boolean }) {
  const [data, setData] = useState<Summary | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const generation = useRef(0);
  const lifetime = useRef(0);
  useEffect(() => {
    let alive = true;
    ++lifetime.current;
    setData(null); setError(null); setBusy(null);
    async function load() {
      const ticket = ++generation.current;
      try {
        const value = await installationAlerts(id);
        if (alive && ticket === generation.current) { setData(value); setError(null); }
      } catch (err) {
        if (!alive || ticket !== generation.current) return;
        if (err instanceof ApiError && err.status === 404) return;
        setError(err instanceof Error ? err.message : "Falha ao consultar alertas.");
      }
    }
    void load();
    const timer = window.setInterval(() => void load(), 15000);
    return () => { alive = false; ++generation.current; ++lifetime.current; window.clearInterval(timer); };
  }, [id]);

  async function ack(alertId: string) {
    const life = lifetime.current;
    setBusy(alertId); setError(null);
    try {
      await acknowledgeAlert(alertId);
      if (life !== lifetime.current) return;
      const ticket = ++generation.current;
      const value = await installationAlerts(id);
      if (ticket === generation.current) setData(value);
    } catch (err) {
      if (life === lifetime.current) setError(err instanceof Error ? err.message : "Falha ao reconhecer.");
    } finally { if (life === lifetime.current) setBusy(null); }
  }

  if (!data && !error) return null;
  return <section aria-label="Monitoramento da instalação">
    <h3>Monitoramento central · Laboratório</h3>
    {error ? <p role="alert" className="error-state">{error}</p> : null}
    {data ? <>
      {!data.configured ? <p>Monitoramento não configurado</p> : null}
      {data.paused ? <p>Agenda pausada. Nenhuma nova ocorrência esperada.</p> : null}
      <dl className="preparation-meta">
        <div><dt>Último backup</dt><dd>{data.last_backup ? `${formatDate(data.last_backup.finished_at)} · ${resultLabels[data.last_backup.status] ?? data.last_backup.status}` : "Nenhum resultado recebido"}</dd></div>
        <div><dt>Última cópia protegida</dt><dd>{data.last_protected_copy ? formatDate(data.last_protected_copy.confirmed_at) : "Nenhum ponto confirmado no vínculo atual"}</dd>
          {data.last_protected_copy ? <dd>A <code>{data.last_protected_copy.snapshot_a}</code><br />B <code>{data.last_protected_copy.snapshot_b}</code></dd> : null}</div>
        <div><dt>Próximo prazo</dt><dd>{data.next_deadline ? formatDate(data.next_deadline) : "Sem cobrança de agenda"}</dd></div>
      </dl>
      {data.alerts.length === 0 ? <p>Nenhuma ocorrência registrada. Isso não comprova que o avaliador esteja ativo.</p> :
        <ul className="central-alerts">{data.alerts.map(alert => <li key={alert.id}>
          <strong>{labels[alert.kind]}</strong> · {alert.resolved_at ? "Resolvido" : "Aberto"}
          <p>{formatDate(alert.created_at)} · {deliveries[alert.delivery_state] ?? "Estado de envio desconhecido"}</p>
          <dl>{Object.entries(alert.detail).map(([key, value]) => <div key={key}><dt>{detailLabels[key] ?? key}</dt><dd>{resultLabels[value] ?? value}</dd></div>)}</dl>
          {alert.acknowledged_at ? <p>Reconhecido em {formatDate(alert.acknowledged_at)}</p> : admin ?
            <button className="btn" disabled={busy !== null} onClick={() => void ack(alert.id)}>{busy === alert.id ? "Registrando…" : "Reconhecer recebimento"}</button> : <p>Aguardando reconhecimento pelo operador.</p>}
        </li>)}</ul>}
    </> : null}
  </section>;
}
