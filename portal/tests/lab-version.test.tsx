import {afterEach,it,expect,vi} from "vitest";
import {act,render,screen} from "@testing-library/react";
import {LabVersion} from "../src/components/LabVersion";
import {json} from "./helpers";
afterEach(()=>{vi.unstubAllGlobals();vi.useRealTimers();});
const data={commit:"a".repeat(40),schema:14,windows:"b".repeat(40),phase:"reconcile",state:"restored",updated_at:"2026-11-19T12:00:00Z",needs_reconciliation:true};
it("mostra versão e bloqueio de recuperação sem comandos do navegador",async()=>{
 vi.stubGlobal("fetch",vi.fn(()=>Promise.resolve(json(data))));render(<LabVersion/>);
 expect(await screen.findByText(/Recuperação aguarda conferência/)).toBeInTheDocument();
 expect(screen.getByText(/esquema 14/)).toBeInTheDocument();expect(screen.queryByRole("button")).not.toBeInTheDocument();
});
it("descarta resposta após sair da página",async()=>{
 let resolve!:(r:Response)=>void;vi.stubGlobal("fetch",vi.fn(()=>new Promise<Response>(r=>{resolve=r;})));
 const page=render(<LabVersion/>);page.unmount();await act(async()=>resolve(json(data)));
 expect(screen.queryByText(/Recuperação aguarda/)).not.toBeInTheDocument();
});
it("falha de consulta não é apresentada como falha do backup",async()=>{
 vi.useFakeTimers();let count=0;vi.stubGlobal("fetch",vi.fn(()=>++count===1?Promise.resolve(json({...data,phase:"open"})):Promise.reject(Error('offline'))));
 render(<LabVersion/>);await act(async()=>{});await act(async()=>{vi.advanceTimersByTime(5000);});
 expect(screen.getByText(/não comprova falha do backup/)).toBeInTheDocument();
});

it("distingue ausência física de pontos do histórico SQL",async()=>{
 vi.stubGlobal("fetch",vi.fn(()=>Promise.resolve(json({...data,phase:"open",inventory_warning:2}))));render(<LabVersion/>);
 expect(await screen.findByRole("alert")).toHaveTextContent("2 pontos históricos ausentes em A");
 expect(screen.getByRole("alert")).toHaveTextContent("restaurar o banco não recria snapshots");
});
