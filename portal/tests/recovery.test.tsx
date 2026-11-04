import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import RecoverySection from "../src/pages/RecoverySection";
import { ApiError, createRecovery, listRecovery, type RecoveryOperation } from "../src/api";

vi.mock("../src/api", async (original) => ({
  ...await original<typeof import("../src/api")>(),
  listRecovery: vi.fn(),
  createRecovery: vi.fn(),
}));

const queryId = "11111111-1111-4111-8111-111111111111";
const snapshotId = "a".repeat(64);
const query: RecoveryOperation = {
  id: queryId, kind: "snapshots", destination_id: "local", revision: 1,
  state: "succeeded", result: { snapshots: [{ id: snapshotId, time: "2026-11-04T12:00:00Z", tags: ["task:task-A"] }] },
};
const restore: RecoveryOperation = {
  id: "22222222-2222-4222-8222-222222222222", kind: "restore",
  destination_id: "local", revision: 1, query_id: queryId, snapshot_id: snapshotId,
  state: "pending",
};
const props = { taskId: "task-A", revision: 1, destinations: [{ id: "local", name: "Disco local" }], admin: true };

beforeEach(() => {
  window.sessionStorage.clear();
  vi.mocked(listRecovery).mockReset().mockResolvedValue([]);
  vi.mocked(createRecovery).mockReset();
});

async function chooseAndConfirm() {
  await screen.findByLabelText("Ponto de recuperação");
  await userEvent.selectOptions(screen.getByLabelText("Ponto de recuperação"), snapshotId);
  await userEvent.click(screen.getByRole("button", { name: "Solicitar restauração" }));
  expect(screen.getByText(/pasta nova e exclusiva sob a raiz autorizada/)).toBeInTheDocument();
}

describe("recuperação assíncrona", () => {
  it("distingue consulta pendente, catálogo vazio e falha", async () => {
    vi.mocked(createRecovery).mockResolvedValue({ ...query, state: "pending", result: undefined });
    render(<RecoverySection {...props} />);
    await screen.findByText(/Nenhuma consulta/);
    await userEvent.click(screen.getByRole("button", { name: "Consultar pontos" }));
    expect(await screen.findByText(/Consulta pendente/)).toBeInTheDocument();
    expect(screen.queryByText(/nenhum ponto disponível/)).toBeNull();
    vi.mocked(listRecovery).mockResolvedValue([{ ...query, result: { snapshots: [] } }]);
    await userEvent.click(screen.getByRole("button", { name: "Atualizar recuperação" }));
    expect(await screen.findByText("Consulta concluída: nenhum ponto disponível.")).toBeInTheDocument();
    vi.mocked(listRecovery).mockResolvedValue([{ ...query, state: "failed", error: "Repositório inacessível", result: undefined }]);
    await userEvent.click(screen.getByRole("button", { name: "Atualizar recuperação" }));
    expect(await screen.findByText(/Falha na consulta: Repositório inacessível/)).toBeInTheDocument();
  });

  it("exige confirmação e só anuncia conclusão confirmada pelo agente", async () => {
    vi.mocked(listRecovery).mockResolvedValue([query]);
    vi.mocked(createRecovery).mockResolvedValue(restore);
    render(<RecoverySection {...props} />);
    await chooseAndConfirm();
    expect(createRecovery).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole("button", { name: "Confirmar restauração" }));
    expect(await screen.findByText(/Restauração — revisão 1, destino local: Pendente/)).toBeInTheDocument();
    expect(screen.queryByText(/Concluída pelo agente/)).toBeNull();
    expect(vi.mocked(createRecovery).mock.calls[0][1]).toMatchObject({
      kind: "restore", destination_id: "local", query_id: queryId, snapshot_id: snapshotId,
    });
    expect(vi.mocked(createRecovery).mock.calls[0][1]).not.toHaveProperty("target");
    vi.mocked(listRecovery).mockResolvedValue([{ ...restore, state: "succeeded", result: { target: "/recovery/operation-2" } }, query]);
    await userEvent.click(screen.getByRole("button", { name: "Atualizar recuperação" }));
    expect(await screen.findByText(/Concluída pelo agente/)).toBeInTheDocument();
    expect(screen.getByText("Pasta no agente: /recovery/operation-2")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Solicitar restauração" })).toBeDisabled();
  });

  it("viewer acompanha sem ações de solicitação", async () => {
    vi.mocked(listRecovery).mockResolvedValue([{ ...restore, state: "running" }, query]);
    render(<RecoverySection {...props} admin={false} />);
    expect(await screen.findByText(/Em execução no agente/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Consultar pontos" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Solicitar restauração" })).toBeNull();
    expect(createRecovery).not.toHaveBeenCalled();
  });

  it("guarda identidade antes do POST e reutiliza após perda de resposta e remontagem", async () => {
    vi.mocked(createRecovery).mockRejectedValue(new ApiError("network", null));
    const first = render(<RecoverySection {...props} />);
    await screen.findByText(/Nenhuma consulta/);
    await userEvent.click(screen.getByRole("button", { name: "Consultar pontos" }));
    await screen.findByRole("alert");
    const firstId = vi.mocked(createRecovery).mock.calls[0][1].id;
    expect(firstId).toMatch(/^[0-9a-f-]{36}$/);
    first.unmount();
    render(<RecoverySection {...props} />);
    await screen.findByText(/Nenhuma consulta/);
    await userEvent.click(screen.getByRole("button", { name: "Consultar pontos" }));
    await waitFor(() => expect(createRecovery).toHaveBeenCalledTimes(2));
    expect(vi.mocked(createRecovery).mock.calls[1][1].id).toBe(firstId);
  });

  it("duplo clique não envia duas restaurações", async () => {
    vi.mocked(listRecovery).mockResolvedValue([query]);
    let complete!: (operation: RecoveryOperation) => void;
    vi.mocked(createRecovery).mockImplementation(() => new Promise((resolve) => { complete = resolve; }));
    render(<RecoverySection {...props} />);
    await chooseAndConfirm();
    const button = screen.getByRole("button", { name: "Confirmar restauração" });
    fireEvent.click(button);
    fireEvent.click(button);
    expect(createRecovery).toHaveBeenCalledTimes(1);
    await act(async () => { complete(restore); });
  });

  it("reenvia restauração com a mesma identidade após resposta perdida e recarga", async () => {
    vi.mocked(listRecovery).mockResolvedValue([query]);
    vi.mocked(createRecovery).mockRejectedValue(new ApiError("network", null));
    const first = render(<RecoverySection {...props} />);
    await chooseAndConfirm();
    await userEvent.click(screen.getByRole("button", { name: "Confirmar restauração" }));
    await screen.findByRole("alert");
    const originalRequest = vi.mocked(createRecovery).mock.calls[0][1];
    first.unmount();
    vi.mocked(createRecovery).mockResolvedValue({ ...restore, id: originalRequest.id });
    render(<RecoverySection {...props} />);
    await chooseAndConfirm();
    await userEvent.click(screen.getByRole("button", { name: "Confirmar restauração" }));
    await waitFor(() => expect(createRecovery).toHaveBeenCalledTimes(2));
    expect(vi.mocked(createRecovery).mock.calls[1][1]).toEqual(originalRequest);
    expect(await screen.findByText(/Restauração deste ponto já solicitada/)).toBeInTheDocument();
  });

  it("consulta periodicamente enquanto o agente ainda não confirmou", async () => {
    vi.useFakeTimers();
    try {
      vi.mocked(listRecovery).mockResolvedValue([{ ...query, state: "pending", result: undefined }]);
      render(<RecoverySection {...props} />);
      await act(async () => { await Promise.resolve(); });
      expect(screen.getByText(/Consulta pendente/)).toBeInTheDocument();
      vi.mocked(listRecovery).mockResolvedValue([{ ...query, result: { snapshots: [] } }]);
      await act(async () => { await vi.advanceTimersByTimeAsync(2000); });
      expect(screen.getByText("Consulta concluída: nenhum ponto disponível.")).toBeInTheDocument();
      expect(listRecovery).toHaveBeenCalledTimes(2);
    } finally {
      vi.useRealTimers();
    }
  });

  it("descarta GET antigo ao trocar de tarefa, inclusive A→B→A", async () => {
    let releaseOld!: (operations: RecoveryOperation[]) => void;
    const old = new Promise<RecoveryOperation[]>((resolve) => { releaseOld = resolve; });
    vi.mocked(listRecovery).mockImplementationOnce(() => old).mockResolvedValue([]);
    const view = render(<RecoverySection {...props} />);
    await waitFor(() => expect(listRecovery).toHaveBeenCalledWith("task-A"));
    view.rerender(<RecoverySection {...props} taskId="task-B" />);
    await waitFor(() => expect(listRecovery).toHaveBeenCalledWith("task-B"));
    view.rerender(<RecoverySection {...props} />);
    await waitFor(() => expect(listRecovery).toHaveBeenCalledTimes(3));
    await act(async () => { releaseOld([{ ...restore, state: "succeeded", result: { target: "/old-result-A" } }, query]); });
    expect(screen.queryByText(/old-result-A/)).toBeNull();
    expect(screen.queryByLabelText("Ponto de recuperação")).toBeNull();
  });

  it("descarta POST antigo e seu finally depois da troca de tarefa", async () => {
    let releaseOld!: (operation: RecoveryOperation) => void;
    vi.mocked(listRecovery).mockResolvedValue([query]);
    vi.mocked(createRecovery).mockImplementation(() => new Promise((resolve) => { releaseOld = resolve; }));
    const view = render(<RecoverySection {...props} />);
    await chooseAndConfirm();
    await userEvent.click(screen.getByRole("button", { name: "Confirmar restauração" }));
    vi.mocked(listRecovery).mockResolvedValue([]);
    view.rerender(<RecoverySection {...props} taskId="task-B" />);
    await screen.findByText(/Nenhuma consulta/);
    await act(async () => { releaseOld({ ...restore, state: "succeeded", result: { target: "/stale-A" } }); });
    expect(screen.queryByText(/stale-A/)).toBeNull();
    expect(screen.getByRole("button", { name: "Consultar pontos" })).toBeEnabled();
  });

  it("trata sessão expirada e erros de restauração como texto", async () => {
    vi.mocked(listRecovery).mockRejectedValue(new ApiError("unauthenticated", 401));
    render(<RecoverySection {...props} />);
    expect(await screen.findByText(/Sessão expirada/)).toBeInTheDocument();
    vi.mocked(listRecovery).mockResolvedValue([
      { ...restore, state: "failed", error: "<img src=x onerror=alert(1)> snapshot removido" },
      { ...query, result: { snapshots: [] } },
    ]);
    await userEvent.click(screen.getByRole("button", { name: "Atualizar recuperação" }));
    expect(await screen.findByText(/snapshot removido/)).toBeInTheDocument();
    expect(screen.queryByRole("img")).toBeNull();
    expect(screen.queryByText(/Concluída pelo agente/)).toBeNull();
  });
});
