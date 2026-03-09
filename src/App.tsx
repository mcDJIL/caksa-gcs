import React, { useState, useEffect, useRef } from "react";
// Pastikan package ini sudah diinstall: npm install @tauri-apps/plugin-shell
import { Command } from "@tauri-apps/plugin-shell"; 
import GCSApp from "./gcs-frontend";
import { Terminal, Cpu, AlertTriangle } from "lucide-react";

function App() {
  const [status, setStatus] = useState<"INIT" | "STARTING" | "READY" | "ERROR">("INIT");
  const [logs, setLogs] = useState<string[]>([]);
  const hasStarted = useRef(false);

  const addLog = (msg: string) => {
    setLogs((prev) => [...prev.slice(-4), msg]); // Keep last 5 logs
  };

  useEffect(() => {
    if (hasStarted.current) return;
    hasStarted.current = true;

    const startBackend = async () => {
      try {
        setStatus("STARTING");
        addLog("Initializing Caksa Backend...");

        // Nama binary harus sesuai dengan konfigurasi tauri.conf.json -> bundle -> externalBin
        // Tauri otomatis menambahkan target triple (-x86_64-pc-windows-msvc.exe) saat runtime
        const command = Command.sidecar("binaries/caksa-backend");
        
        command.stdout.on("data", (line) => {
          console.log(`[PY]: ${line}`);
          if (line.includes("WebSocket listening") || line.includes("WS Server") || line.includes("Serving")) {
            setStatus("READY");
          }
        });

        command.stderr.on("data", (line) => console.error(`[PY ERR]: ${line}`));

        addLog("Spawning process...");
        const child = await command.spawn();
        addLog(`Backend PID: ${child.pid}`);

        // Fallback safety: Jika dalam 3 detik tidak ada log 'listening', anggap siap (untuk kasus buffer delay)
        setTimeout(() => {
            setStatus((s) => s === "STARTING" ? "READY" : s);
        }, 3000);

      } catch (error) {
        console.error("Gagal start backend:", error);
        addLog(`CRITICAL ERROR: ${JSON.stringify(error)}`);
        setStatus("ERROR");
      }
    };

    startBackend();
  }, []);

  if (status === "ERROR") {
    return (
      <div className="flex h-screen w-screen bg-gray-900 text-red-500 items-center justify-center flex-col gap-4 p-10 text-center">
        <AlertTriangle size={64} />
        <h1 className="text-2xl font-bold">Backend Initialization Failed</h1>
        <p className="text-gray-400">Ensure the Python binary exists in <code>src-tauri/binaries/</code> and matches your OS architecture.</p>
        <div className="bg-black p-4 rounded text-left font-mono text-xs w-full max-w-lg overflow-auto border border-red-900/50">
            {logs.map((l, i) => <div key={i} className="text-red-400"> {l}</div>)}
        </div>
      </div>
    );
  }

  if (status !== "READY") {
    return (
      <div className="flex h-screen w-screen bg-gray-900 text-white items-center justify-center flex-col gap-6">
        <div className="relative">
            <div className="w-24 h-24 border-4 border-blue-600 border-t-transparent rounded-full animate-spin"></div>
            <div className="absolute inset-0 flex items-center justify-center">
                <Cpu size={32} className="text-blue-400 animate-pulse"/>
            </div>
        </div>
        
        <div className="text-center space-y-2">
            <h1 className="text-3xl font-bold tracking-widest">CAKSA<span className="text-blue-500">GCS</span></h1>
            <p className="text-gray-500 font-mono text-sm animate-pulse">INITIALIZING CORE SYSTEMS...</p>
        </div>

        <div className="w-96 bg-gray-800 rounded-lg p-3 font-mono text-xs text-green-400 border border-gray-700 h-32 overflow-hidden flex flex-col justify-end shadow-xl">
            <div className="flex items-center gap-2 text-gray-500 mb-2 border-b border-gray-700 pb-1">
                <Terminal size={12}/> SYSTEM LOG
            </div>
            {logs.map((l, i) => (
                <div key={i} className="truncate"> {l}</div>
            ))}
        </div>
      </div>
    );
  }

  return <GCSApp />;
}

export default App;