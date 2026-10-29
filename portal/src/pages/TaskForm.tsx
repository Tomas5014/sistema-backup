import { useState, type FormEvent } from "react";

/** Formulário estruturado do envelope da revisão (contrato v1).
 *
 * Monta {name, spec, schedule} — nunca JSON manual e nunca segredo:
 * os campos *_environment nomeiam a custódia no agente.
 * O JSON exportado depois herda id/cliente/dispositivo dos UUIDs
 * centrais, decididos no servidor.
 */

export interface SmbForm {
  server: string;
  share: string;
  mount_point: string;
  marker: string;
  probe_timeout_seconds: string;
}

export interface SftpForm {
  host: string;
  port: string;
  user: string;
  remote_path: string;
  identity_file: string;
  known_hosts_file: string;
  connect_timeout_seconds: string;
  server_alive_interval_seconds: string;
  server_alive_count_max: string;
}

export interface S3Form {
  endpoint: string;
  bucket: string;
  access_key_environment: string;
  secret_key_environment: string;
  prefix: string;
  region: string;
  bucket_lookup: string;
  storage_class: string;
}

export interface DestForm {
  id: string;
  name: string;
  kind: "local" | "sftp" | "smb" | "s3";
  repository: string;
  password_environment: string;
  keep_last: string;
  sftp: SftpForm;
  smb: SmbForm;
  s3: S3Form;
}

export interface SrcForm {
  path: string;
  smb: SmbForm | null;
}

export interface TaskFormState {
  name: string;
  sources: SrcForm[];
  exclusions: string[];
  compression: "auto" | "off" | "max";
  destinations: DestForm[];
  timeouts: {
    initialize_seconds: string;
    backup_seconds: string;
    snapshots_seconds: string;
    check_seconds: string;
    restore_seconds: string;
    forget_seconds: string;
  };
  retry: { attempts: string; backoff_seconds: string };
  schedule: {
    kind: "manual" | "hourly" | "daily" | "weekly";
    timezone: string;
    hour: string;
    minute: string;
    weekdays: number[];
  };
}

const emptySmb = (): SmbForm => ({
  server: "", share: "", mount_point: "", marker: "",
  probe_timeout_seconds: "10",
});

const emptySftp = (): SftpForm => ({
  host: "", port: "22", user: "", remote_path: "", identity_file: "",
  known_hosts_file: "", connect_timeout_seconds: "15",
  server_alive_interval_seconds: "60", server_alive_count_max: "3",
});

const emptyS3 = (): S3Form => ({
  endpoint: "", bucket: "", access_key_environment: "",
  secret_key_environment: "", prefix: "", region: "", bucket_lookup: "",
  storage_class: "",
});

const emptyDest = (): DestForm => ({
  id: "", name: "", kind: "local", repository: "",
  password_environment: "", keep_last: "", sftp: emptySftp(),
  smb: emptySmb(), s3: emptyS3(),
});

export const emptyTask = (): TaskFormState => ({
  name: "",
  sources: [{ path: "", smb: null }],
  exclusions: [],
  compression: "auto",
  destinations: [emptyDest()],
  timeouts: {
    initialize_seconds: "120", backup_seconds: "14400",
    snapshots_seconds: "120", check_seconds: "3600",
    restore_seconds: "14400", forget_seconds: "3600",
  },
  retry: { attempts: "1", backoff_seconds: "0" },
  schedule: {
    kind: "daily", timezone: "America/Sao_Paulo",
    hour: "2", minute: "0", weekdays: [],
  },
});

const num = (v: string) => (v === "" ? undefined : Number(v));
const txt = (v: string) => (v === "" ? undefined : v);

function smbToJson(s: SmbForm) {
  return {
    server: s.server, share: s.share, mount_point: s.mount_point,
    ...(txt(s.marker) && { marker: s.marker }),
    ...(num(s.probe_timeout_seconds) !== undefined && {
      probe_timeout_seconds: Number(s.probe_timeout_seconds),
    }),
  };
}

/** Estado do form → envelope da revisão (contrato central v1). */
export function serializeTask(f: TaskFormState): Record<string, unknown> {
  const destinations = f.destinations.map((d) => {
    const base: Record<string, unknown> = {
      id: d.id, name: d.name, kind: d.kind,
      password_environment: d.password_environment,
    };
    if (d.keep_last !== "") {
      base.retention = { keep_last: Number(d.keep_last) };
    }
    if (d.kind === "local" || d.kind === "smb") {
      base.repository = d.repository;
    }
    if (d.kind === "sftp") {
      base.sftp = {
        host: d.sftp.host, user: d.sftp.user,
        remote_path: d.sftp.remote_path,
        identity_file: d.sftp.identity_file,
        known_hosts_file: d.sftp.known_hosts_file,
        ...(num(d.sftp.port) !== undefined && { port: Number(d.sftp.port) }),
        ...(num(d.sftp.connect_timeout_seconds) !== undefined && {
          connect_timeout_seconds: Number(d.sftp.connect_timeout_seconds),
        }),
        ...(num(d.sftp.server_alive_interval_seconds) !== undefined && {
          server_alive_interval_seconds:
            Number(d.sftp.server_alive_interval_seconds),
        }),
        ...(num(d.sftp.server_alive_count_max) !== undefined && {
          server_alive_count_max: Number(d.sftp.server_alive_count_max),
        }),
      };
    }
    if (d.kind === "smb") base.smb = smbToJson(d.smb);
    if (d.kind === "s3") {
      base.s3 = {
        endpoint: d.s3.endpoint, bucket: d.s3.bucket,
        access_key_environment: d.s3.access_key_environment,
        secret_key_environment: d.s3.secret_key_environment,
        ...(txt(d.s3.prefix) && { prefix: d.s3.prefix }),
        ...(txt(d.s3.region) && { region: d.s3.region }),
        ...(txt(d.s3.bucket_lookup) && { bucket_lookup: d.s3.bucket_lookup }),
        ...(txt(d.s3.storage_class) && {
          storage_class: d.s3.storage_class,
        }),
      };
    }
    return base;
  });

  const schedule: Record<string, unknown> = { kind: f.schedule.kind };
  if (f.schedule.kind !== "manual") {
    schedule.timezone = f.schedule.timezone;
    if (f.schedule.minute !== "") schedule.minute = Number(f.schedule.minute);
    if (f.schedule.kind !== "hourly" && f.schedule.hour !== "") {
      schedule.hour = Number(f.schedule.hour);
    }
    if (f.schedule.kind === "weekly") schedule.weekdays = f.schedule.weekdays;
  }

  const timeouts = Object.fromEntries(
    Object.entries(f.timeouts).flatMap(([key, v]) =>
      v === "" ? [] : [[key, Number(v)]],
    ),
  );
  const retry: Record<string, unknown> = {};
  if (f.retry.attempts !== "") retry.attempts = Number(f.retry.attempts);
  if (f.retry.backoff_seconds !== "") {
    retry.backoff_seconds = Number(f.retry.backoff_seconds);
  }

  return {
    name: f.name,
    spec: {
      sources: f.sources.map((s) => ({
        path: s.path,
        ...(s.smb ? { smb: smbToJson(s.smb) } : {}),
      })),
      exclusions: f.exclusions.filter((e) => e !== ""),
      compression: f.compression,
      destinations,
      ...(Object.keys(timeouts).length && { timeouts }),
      ...(Object.keys(retry).length && { retry }),
    },
    schedule,
  };
}

/** Revisão salva → estado do form (edição). */
export function formFromConfig(config: Record<string, unknown>): TaskFormState {
  const spec = (config.spec ?? {}) as Record<string, unknown>;
  const schedule = (config.schedule ?? { kind: "manual" }) as Record<
    string, unknown
  >;
  const d: TaskFormState = emptyTask();
  d.name = String(config.name ?? "");
  d.compression = (spec.compression as TaskFormState["compression"]) ?? "auto";
  d.sources = ((spec.sources ?? []) as Record<string, unknown>[]).map(
    (s) => ({
      path: String(s.path ?? ""),
      smb: s.smb ? { ...emptySmb(), ...(s.smb as object) } : null,
    }),
  );
  if (!d.sources.length) d.sources = [{ path: "", smb: null }];
  d.exclusions = (spec.exclusions as string[]) ?? [];
  const timeoutData = (spec.timeouts ?? {}) as Record<string, number>;
  d.timeouts = Object.fromEntries(
    Object.entries(d.timeouts).map(([key, v]) => [
      key,
      String(timeoutData[key] ?? v),
    ]),
  ) as TaskFormState["timeouts"];
  const retryData = (spec.retry ?? {}) as Record<string, number>;
  d.retry = {
    attempts: String(retryData.attempts ?? 1),
    backoff_seconds: String(retryData.backoff_seconds ?? 0),
  };
  d.destinations = (
    (spec.destinations ?? []) as Record<string, unknown>[]
  ).map((row) => {
    const base = emptyDest();
    const keep = (row.retention as Record<string, unknown> | undefined)
      ?.keep_last;
    return {
      ...base,
      id: String(row.id ?? ""),
      name: String(row.name ?? ""),
      kind: (row.kind as DestForm["kind"]) ?? "local",
      repository: String(row.repository ?? ""),
      password_environment: String(row.password_environment ?? ""),
      keep_last: keep == null ? "" : String(keep),
      sftp: { ...emptySftp(), ...(row.sftp as object ?? {}) },
      smb: { ...emptySmb(), ...(row.smb as object ?? {}) },
      s3: { ...emptyS3(), ...(row.s3 as object ?? {}) },
    };
  });
  if (!d.destinations.length) d.destinations = [emptyDest()];
  const wd = (schedule.weekdays as number[]) ?? [];
  d.schedule = {
    kind: (schedule.kind as TaskFormState["schedule"]["kind"]) ?? "manual",
    timezone: String(schedule.timezone ?? "America/Sao_Paulo"),
    hour: schedule.hour == null ? "" : String(schedule.hour),
    minute: schedule.minute == null ? "" : String(schedule.minute),
    weekdays: wd.map(String).map(Number),
  };
  return d;
}

const WEEKDAYS = [
  [1, "seg"], [2, "ter"], [3, "qua"], [4, "qui"],
  [5, "sex"], [6, "sáb"], [7, "dom"],
] as const;

export default function TaskForm({
  initial,
  onSubmit,
  pending,
  error,
  submitLabel,
}: {
  initial: TaskFormState;
  onSubmit: (config: Record<string, unknown>) => void;
  pending: boolean;
  error: string | null;
  submitLabel: string;
}) {
  const [form, setForm] = useState<TaskFormState>(initial);
  const set = (patch: Partial<TaskFormState>) =>
    setForm((f) => ({ ...f, ...patch }));
  const setSrc = (i: number, patch: Partial<SrcForm>) =>
    setForm((f) => ({
      ...f,
      sources: f.sources.map((s, j) => (j === i ? { ...s, ...patch } : s)),
    }));
  const setDest = (i: number, patch: Partial<DestForm>) =>
    setForm((f) => ({
      ...f,
      destinations: f.destinations.map((d, j) =>
        j === i ? { ...d, ...patch } : d,
      ),
    }));

  function submit(ev: FormEvent) {
    ev.preventDefault();
    if (pending) return;
    onSubmit(serializeTask(form));
  }

  return (
    <form onSubmit={submit} className="stack">
      {error ? <p role="alert" className="error">{error}</p> : null}

      <label>
        Nome da tarefa
        <input
          aria-label="Nome da tarefa"
          value={form.name}
          onChange={(e) => set({ name: e.target.value })}
          required
        />
      </label>

      <section>
        <h2>Origens</h2>
        {form.sources.map((src, i) => (
          <fieldset key={i} className="box">
            <legend>Origem {i + 1}</legend>
            <label>
              Caminho no agente Linux
              <input
                aria-label={`Caminho da origem ${i + 1}`}
                value={src.path}
                onChange={(e) => setSrc(i, { path: e.target.value })}
                placeholder="/dados/financeiro"
                required
              />
            </label>
            <label className="inline">
              <input
                type="checkbox"
                checked={src.smb !== null}
                onChange={(e) =>
                  setSrc(i, { smb: e.target.checked ? emptySmb() : null })
                }
              />
              origem em volume SMB montado
            </label>
            {src.smb ? (
              <div className="grid">
                <input aria-label={`Servidor SMB ${i + 1}`} placeholder="servidor"
                  value={src.smb.server}
                  onChange={(e) =>
                    setSrc(i, { smb: { ...src.smb!, server: e.target.value } })
                  } />
                <input aria-label={`Compartilhamento ${i + 1}`}
                  placeholder="compartilhamento"
                  value={src.smb.share}
                  onChange={(e) =>
                    setSrc(i, { smb: { ...src.smb!, share: e.target.value } })
                  } />
                <input aria-label={`Ponto de montagem ${i + 1}`}
                  placeholder="/mnt/compartilhamento"
                  value={src.smb.mount_point}
                  onChange={(e) => setSrc(i, {
                    smb: { ...src.smb!, mount_point: e.target.value },
                  })} />
                <input aria-label={`Marcador SMB ${i + 1}`}
                  placeholder="marcador (opcional)"
                  value={src.smb.marker}
                  onChange={(e) =>
                    setSrc(i, { smb: { ...src.smb!, marker: e.target.value } })
                  } />
              </div>
            ) : null}
            {form.sources.length > 1 ? (
              <button type="button" onClick={() =>
                set({ sources: form.sources.filter((_, j) => j !== i) })
              }>Remover origem</button>
            ) : null}
          </fieldset>
        ))}
        <button type="button" onClick={() =>
          set({ sources: [...form.sources, { path: "", smb: null }] })
        }>+ Origem</button>
      </section>

      <section>
        <h2>Exclusões</h2>
        {form.exclusions.map((pattern, i) => (
          <div key={i} className="row">
            <input aria-label={`Exclusão ${i + 1}`} value={pattern}
              placeholder="*.tmp"
              onChange={(e) => set({
                exclusions: form.exclusions.map((p, j) =>
                  j === i ? e.target.value : p),
              })} />
            <button type="button" onClick={() => set({
              exclusions: form.exclusions.filter((_, j) => j !== i),
            })}>Remover</button>
          </div>
        ))}
        <button type="button" onClick={() =>
          set({ exclusions: [...form.exclusions, ""] })
        }>+ Exclusão</button>
      </section>

      <label>
        Compressão
        <select
          aria-label="Compressão"
          value={form.compression}
          onChange={(e) =>
            set({ compression: e.target.value as TaskFormState["compression"] })
          }
        >
          <option value="auto">auto</option>
          <option value="off">off</option>
          <option value="max">max</option>
        </select>
      </label>

      <section>
        <h2>Destinos</h2>
        {form.destinations.map((dest, i) => (
          <fieldset key={i} className="box">
            <legend>Destino {i + 1}</legend>
            <div className="grid">
              <input aria-label={`Identificador do destino ${i + 1}`}
                placeholder="id (sem espaços)" value={dest.id}
                onChange={(e) => setDest(i, { id: e.target.value })} />
              <input aria-label={`Nome do destino ${i + 1}`}
                placeholder="nome" value={dest.name}
                onChange={(e) => setDest(i, { name: e.target.value })} />
              <select aria-label={`Tipo do destino ${i + 1}`} value={dest.kind}
                onChange={(e) =>
                  setDest(i, { kind: e.target.value as DestForm["kind"] })
                }>
                <option value="local">local</option>
                <option value="sftp">sftp</option>
                <option value="smb">smb</option>
                <option value="s3">s3</option>
              </select>
            </div>
            {(dest.kind === "local" || dest.kind === "smb") && (
              <label>
                Repositório {dest.kind === "local" ? "(caminho absoluto)" : "(relativo ao montante)"}
                <input
                  aria-label={`Repositório do destino ${i + 1}`}
                  value={dest.repository}
                  onChange={(e) =>
                    setDest(i, { repository: e.target.value })
                  }
                />
              </label>
            )}
            <label>
              Senha do repositório — nome na custódia do agente
              <input
                aria-label={`password_environment do destino ${i + 1}`}
                value={dest.password_environment}
                onChange={(e) =>
                  setDest(i, { password_environment: e.target.value })
                }
                placeholder="MACIP_RESTIC_PASSWORD"
              />
            </label>
            <label>
              Retenção — keep_last (opcional)
              <input
                aria-label={`keep_last do destino ${i + 1}`}
                inputMode="numeric"
                value={dest.keep_last}
                onChange={(e) => setDest(i, { keep_last: e.target.value })}
                placeholder="ex.: 7"
              />
            </label>
            {dest.kind === "sftp" && (
              <div className="grid">
                <input aria-label={`SFTP host ${i + 1}`} placeholder="host"
                  value={dest.sftp.host}
                  onChange={(e) => setDest(i, {
                    sftp: { ...dest.sftp, host: e.target.value },
                  })} />
                <input aria-label={`SFTP porta ${i + 1}`} placeholder="porta"
                  inputMode="numeric" value={dest.sftp.port}
                  onChange={(e) => setDest(i, {
                    sftp: { ...dest.sftp, port: e.target.value },
                  })} />
                <input aria-label={`SFTP usuário ${i + 1}`} placeholder="usuário"
                  value={dest.sftp.user}
                  onChange={(e) => setDest(i, {
                    sftp: { ...dest.sftp, user: e.target.value },
                  })} />
                <input aria-label={`SFTP caminho remoto ${i + 1}`}
                  placeholder="caminho remoto" value={dest.sftp.remote_path}
                  onChange={(e) => setDest(i, {
                    sftp: { ...dest.sftp, remote_path: e.target.value },
                  })} />
                <input aria-label={`SFTP identity file ${i + 1}`}
                  placeholder="identity_file" value={dest.sftp.identity_file}
                  onChange={(e) => setDest(i, {
                    sftp: { ...dest.sftp, identity_file: e.target.value },
                  })} />
                <input aria-label={`SFTP known hosts ${i + 1}`}
                  placeholder="known_hosts_file"
                  value={dest.sftp.known_hosts_file}
                  onChange={(e) => setDest(i, {
                    sftp: { ...dest.sftp, known_hosts_file: e.target.value },
                  })} />
              </div>
            )}
            {dest.kind === "smb" && (
              <div className="grid">
                <input aria-label={`SMB servidor ${i + 1}`} placeholder="servidor"
                  value={dest.smb.server}
                  onChange={(e) => setDest(i, {
                    smb: { ...dest.smb, server: e.target.value },
                  })} />
                <input aria-label={`SMB share ${i + 1}`}
                  placeholder="compartilhamento" value={dest.smb.share}
                  onChange={(e) => setDest(i, {
                    smb: { ...dest.smb, share: e.target.value },
                  })} />
                <input aria-label={`SMB mount ${i + 1}`}
                  placeholder="ponto de montagem" value={dest.smb.mount_point}
                  onChange={(e) => setDest(i, {
                    smb: { ...dest.smb, mount_point: e.target.value },
                  })} />
                <input aria-label={`SMB marcador ${i + 1}`}
                  placeholder="marcador (opcional)" value={dest.smb.marker}
                  onChange={(e) => setDest(i, {
                    smb: { ...dest.smb, marker: e.target.value },
                  })} />
              </div>
            )}
            {dest.kind === "s3" && (
              <div className="grid">
                <input aria-label={`S3 endpoint ${i + 1}`} placeholder="https://endpoint"
                  value={dest.s3.endpoint}
                  onChange={(e) => setDest(i, {
                    s3: { ...dest.s3, endpoint: e.target.value },
                  })} />
                <input aria-label={`S3 bucket ${i + 1}`} placeholder="bucket"
                  value={dest.s3.bucket}
                  onChange={(e) => setDest(i, {
                    s3: { ...dest.s3, bucket: e.target.value },
                  })} />
                <input aria-label={`S3 access env ${i + 1}`}
                  placeholder="access_key_environment"
                  value={dest.s3.access_key_environment}
                  onChange={(e) => setDest(i, {
                    s3: { ...dest.s3, access_key_environment: e.target.value },
                  })} />
                <input aria-label={`S3 secret env ${i + 1}`}
                  placeholder="secret_key_environment"
                  value={dest.s3.secret_key_environment}
                  onChange={(e) => setDest(i, {
                    s3: { ...dest.s3, secret_key_environment: e.target.value },
                  })} />
                <input aria-label={`S3 prefixo ${i + 1}`}
                  placeholder="prefix (opcional)" value={dest.s3.prefix}
                  onChange={(e) => setDest(i, {
                    s3: { ...dest.s3, prefix: e.target.value },
                  })} />
                <input aria-label={`S3 região ${i + 1}`}
                  placeholder="region (opcional)" value={dest.s3.region}
                  onChange={(e) => setDest(i, {
                    s3: { ...dest.s3, region: e.target.value },
                  })} />
              </div>
            )}
            {form.destinations.length > 1 ? (
              <button type="button" onClick={() => set({
                destinations: form.destinations.filter((_, j) => j !== i),
              })}>Remover destino</button>
            ) : null}
          </fieldset>
        ))}
        <button type="button" onClick={() =>
          set({ destinations: [...form.destinations, emptyDest()] })
        }>+ Destino</button>
      </section>

      <section>
        <h2>Agendamento cadastrado</h2>
        <label>
          Frequência
          <select
            aria-label="Frequência"
            value={form.schedule.kind}
            onChange={(e) => set({
              schedule: {
                ...form.schedule,
                kind: e.target.value as TaskFormState["schedule"]["kind"],
              },
            })}
          >
            <option value="manual">manual</option>
            <option value="hourly">horária</option>
            <option value="daily">diária</option>
            <option value="weekly">semanal</option>
          </select>
        </label>
        {form.schedule.kind !== "manual" && (
          <>
            <label>
              Fuso horário (IANA)
              <input
                aria-label="Fuso horário"
                value={form.schedule.timezone}
                onChange={(e) => set({
                  schedule: { ...form.schedule, timezone: e.target.value },
                })}
                placeholder="America/Sao_Paulo"
              />
            </label>
            {form.schedule.kind !== "hourly" && (
              <label>
                Hora (0–23, local)
                <input
                  aria-label="Hora"
                  inputMode="numeric"
                  value={form.schedule.hour}
                  onChange={(e) => set({
                    schedule: { ...form.schedule, hour: e.target.value },
                  })}
                />
              </label>
            )}
            <label>
              Minuto (0–59)
              <input
                aria-label="Minuto"
                inputMode="numeric"
                value={form.schedule.minute}
                onChange={(e) => set({
                  schedule: { ...form.schedule, minute: e.target.value },
                })}
              />
            </label>
            {form.schedule.kind === "weekly" && (
              <fieldset>
                <legend>Dias da semana (1=segunda … 7=domingo)</legend>
                {WEEKDAYS.map(([day, label]) => (
                  <label key={day} className="inline">
                    <input
                      type="checkbox"
                      checked={form.schedule.weekdays.includes(day)}
                      onChange={(e) => set({
                        schedule: {
                          ...form.schedule,
                          weekdays: e.target.checked
                            ? [...form.schedule.weekdays, day]
                            : form.schedule.weekdays.filter(
                                (d) => d !== day,
                              ),
                        },
                      })}
                    />
                    {label}
                  </label>
                ))}
              </fieldset>
            )}
          </>
        )}
      </section>

      <details>
        <summary>Avançado — timeouts e repetição</summary>
        <div className="grid">
          {(
            [
              ["initialize_seconds", "Inicialização (s)"],
              ["backup_seconds", "Backup (s)"],
              ["snapshots_seconds", "Snapshots (s)"],
              ["check_seconds", "Verificação (s)"],
              ["restore_seconds", "Restore (s)"],
              ["forget_seconds", "Esquecer (s)"],
            ] as const
          ).map(([key, label]) => (
            <label key={key}>
              {label}
              <input
                aria-label={label}
                inputMode="numeric"
                value={form.timeouts[key]}
                onChange={(e) => set({
                  timeouts: { ...form.timeouts, [key]: e.target.value },
                })}
              />
            </label>
          ))}
          <label>
            Tentativas por destino
            <input
              aria-label="Tentativas por destino"
              inputMode="numeric"
              value={form.retry.attempts}
              onChange={(e) => set({
                retry: { ...form.retry, attempts: e.target.value },
              })}
            />
          </label>
          <label>
            Espera entre tentativas (s)
            <input
              aria-label="Espera entre tentativas"
              inputMode="numeric"
              value={form.retry.backoff_seconds}
              onChange={(e) => set({
                retry: { ...form.retry, backoff_seconds: e.target.value },
              })}
            />
          </label>
        </div>
      </details>

      <button type="submit" disabled={pending}>
        {pending ? "Salvando…" : submitLabel}
      </button>
    </form>
  );
}
