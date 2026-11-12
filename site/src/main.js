// Mobile navigation and FAQ deep links.
(() => {
  // --- Menu: the button opens and closes the nav; it also closes when focus leaves the header.
  const button = document.querySelector(".menu-button");
  const nav = document.getElementById("site-nav");
  const header = document.querySelector(".site-header");

  if (button && nav && header) {
    const setOpen = (open) => {
      button.setAttribute("aria-expanded", String(open));
      nav.classList.toggle("is-open", open);
    };

    button.addEventListener("click", () => setOpen(button.getAttribute("aria-expanded") !== "true"));
    nav.addEventListener("click", (event) => {
      if (event.target.closest("a")) setOpen(false);
    });
    header.addEventListener("focusout", (event) => {
      // relatedTarget is null when a tap lands on blank space inside the panel: keep it open then.
      if (event.relatedTarget && !header.contains(event.relatedTarget)) setOpen(false);
    });
    document.addEventListener("click", (event) => {
      if (nav.classList.contains("is-open") && !header.contains(event.target)) setOpen(false);
    });
    document.addEventListener("keydown", (event) => {
      if (event.key !== "Escape" || !nav.classList.contains("is-open")) return;
      const focusWasInMenu = nav.contains(document.activeElement);
      setOpen(false);
      if (focusWasInMenu) button.focus();
    });
  }

  // --- FAQ deep links: the link targets a question's <summary> (which can take focus, so the
  // browser's own fragment navigation moves focus there); this opens the surrounding <details>.
  const openDetails = (id) => {
    const details = id && document.getElementById(id)?.closest("details");
    if (details) details.open = true;
  };

  document.addEventListener("click", (event) => {
    const link = event.target.closest('a[href^="#"]');
    if (link) openDetails(link.getAttribute("href").slice(1));
  });
  window.addEventListener("hashchange", () => openDetails(location.hash.slice(1)));
  openDetails(location.hash.slice(1));
})();
