// ---- Realtime přes Socket.IO ----
(function () {
  try {
    const socket = io({ transports: ["websocket", "polling"] });

    // Pomůcka: bezpečné reloadování (nethltej to při skrytém tabu)
    let reloadPending = false;
    function safeReload() {
      if (document.hidden) { reloadPending = true; return; }
      window.location.reload();
    }
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden && reloadPending) { reloadPending = false; window.location.reload(); }
    });

    // Události ze serveru (viz app.py: socketio.emit(...))
    const handlers = [
      "akce_updated",
      "akce_deleted",
      "inventory_updated",
      "product_updated",
      "checklist_updated",
      "timesheet_updated"
    ];
    handlers.forEach(evt => socket.on(evt, () => {
      // Jemná filtrace podle stránky (můžeš upravit dle potřeby)
      const path = location.pathname;
      if (
        path.startsWith("/akce") ||
        path.startsWith("/sklad") ||
        path.startsWith("/produkty") ||
        path.startsWith("/hodiny") ||
        path === "/" ||
        path === "/index"
      ) {
        safeReload();
      }
    }));
  } catch (e) {
    console.warn("Socket.IO se nepodařilo inicializovat:", e);
  }
})();

// ---- PWA: registrace service workeru ----
(function () {
  if ("serviceWorker" in navigator) {
    window.addEventListener("load", async () => {
      try {
        await navigator.serviceWorker.register("/static/sw.js");
        // console.log("SW registrován");
      } catch (e) {
        console.warn("SW registrace selhala:", e);
      }
    });
  }
})();
