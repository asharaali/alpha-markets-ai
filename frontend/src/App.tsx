import { Route, Routes } from "react-router";
import { SessionProvider, useSession } from "./auth";
import { isDemo } from "./lib/api";
import { ComingSoon } from "./pages/ComingSoon";
import { Home } from "./pages/Home";
import { SECTIONS } from "./sections";
import { DemoBanner } from "./shell/DemoBanner";
import { LoginScreen } from "./shell/LoginScreen";
import { Ticker } from "./shell/Ticker";
import { TopBar } from "./shell/TopBar";

function Terminal() {
  const { user, loading } = useSession();
  const demo = isDemo();
  const base = demo ? "/demo" : "";
  if (loading) return <div className="boot mono">ALPHA MARKETS TERMINAL</div>;
  if (!user) return <LoginScreen />;
  return (
    <div className="terminal">
      {demo && <DemoBanner />}
      <TopBar base={base} />
      <Ticker />
      <main className="content">
        <Routes>
          {SECTIONS.map((s) => (
            <Route key={s.path} path={`${base}/${s.path}`}
              element={s.module === null ? <Home /> : <ComingSoon section={s} />} />
          ))}
          <Route path="*" element={<Home />} />
        </Routes>
      </main>
    </div>
  );
}

export function App() {
  return <SessionProvider><Terminal /></SessionProvider>;
}
