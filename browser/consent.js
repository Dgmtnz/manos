// Banners de cookies: pulsa «rechazar» o «solo necesarias», NUNCA «aceptar». Si el banner no tiene esa opción, no
// toca nada (decide el agente o el usuario). Corre en cada frame (`context.add_init_script` en session.py), mira
// las mutaciones del DOM hasta 10 s después de `load` (30 s como mucho) y deja el CMP que rechazó en
// `<html data-manos-consent>`, que es lo que lee la cosecha de observe.py. Sin globales, sin parches de prototipos.
(() => {
  // Los ganchos estables de cada CMP: el botón de rechazo por su id o clase documentados.
  const CMP = [
    ['onetrust', '#onetrust-reject-all-handler'],
    ['cookiebot', '#CybotCookiebotDialogBodyButtonDecline'],
    ['didomi', '#didomi-notice-disagree-button'],
    ['usercentrics', '[data-testid="uc-deny-all-button"]'],
    ['cookieyes', '.cky-btn-reject'],
    ['complianz', '.cmplz-btn.cmplz-deny'],
    ['iubenda', '.iubenda-cs-reject-btn'],
    ['klaro', '.klaro .cn-decline'],
    ['trustarc', '#truste-consent-required'],
  ];
  // Los que no tienen id de rechazo: su contenedor, y dentro el botón por su texto. El genérico, al final.
  // El genérico es estrecho a propósito: «consent» casa «consentimiento» (el paso de un trámite con Acepto / No
  // acepto), «cmp» los `cmp-container` de AEM que envuelven páginas enteras y «privacy» las páginas de ajustes.
  const ZONA = ['cookie', 'gdpr']
    .flatMap(w => [`[id*="${w}" i]`, `[class*="${w}" i]`, `[aria-label*="${w}" i]`]).join(',');
  // Un campo de formulario dice que no es un banner (las casillas de un panel de cookies sí valen).
  const CAMPO = 'input:not([type=checkbox]):not([type=hidden]):not([type=button]):not([type=submit]),select,textarea';
  const esBanner = caja => !caja.querySelector(CAMPO) && !caja.shadowRoot?.querySelector(CAMPO) &&
    /cookie/i.test(caja.innerText + ' ' + (caja.shadowRoot?.textContent || ''));
  const CAJA = [['quantcast', '.qc-cmp2-container'], ['usercentrics', '#usercentrics-cmp-ui'], ['generico', ZONA]];
  const SOMBRA = '#usercentrics-root,#usercentrics-cmp-ui,' + ZONA;  // hosts cuyo shadow root se mira
  const BOTON = 'button,[role=button],a,input[type=button],input[type=submit]';
  const NO = new RegExp('^(' + [
    'rechazar( todo| todas)?( las)?( cookies)?( opcionales| no necesarias)?', 'no acepto', 'denegar( todo| todas)?',
    'continuar sin aceptar', '(aceptar |usar |permitir )?(solo|sólo|únicamente) (las )?(cookies )?(necesarias|esenciales)',
    'reject( all)?( cookies)?', 'decline( all)?( cookies)?', 'deny( all)?', 'disagree', 'continue without accepting',
    '(accept |allow |use )?only (strictly )?(necessary|essential)( cookies)?', '(strictly )?necessary( cookies)? only',
  ].join('|') + ')$', 'i');

  const MO = MutationObserver, later = setTimeout, nunca = clearTimeout;
  let hecho = false, timer = 0, otra = 0;
  const visible = el => {
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0 && el.checkVisibility({checkVisibilityCSS: true});
  };
  const texto = el => (el.tagName === 'INPUT' ? el.value : el.innerText || el.getAttribute('aria-label') || '')
    .replace(/\s+/g, ' ').trim();

  // true si pulsó; `oculto` dice que hay candidato aún sin pintar (un banner que entra con transición).
  let oculto = false;
  const pulsa = (el, cmp) => {
    if (!visible(el)) { oculto = true; return false; }
    el.click();
    document.documentElement.setAttribute('data-manos-consent', cmp);
    return true;
  };
  const busca = () => {
    oculto = false;
    const raices = [document];
    for (const h of document.querySelectorAll(SOMBRA)) if (h.shadowRoot) raices.push(h.shadowRoot);
    for (const raiz of raices)
      for (const [cmp, sel] of CMP) for (const el of raiz.querySelectorAll(sel)) if (pulsa(el, cmp)) return true;
    for (const raiz of raices)
      for (const [cmp, sel] of CAJA)
        for (const caja of raiz.querySelectorAll(sel)) {
          if (caja === document.body || caja === document.documentElement) continue;
          if (cmp === 'generico' && !esBanner(caja)) continue;
          const botones = [...caja.querySelectorAll(BOTON), ...(caja.shadowRoot?.querySelectorAll(BOTON) || [])];
          for (const el of botones) if (NO.test(texto(el)) && pulsa(el, cmp)) return true;
        }
    return false;
  };

  const para = () => { hecho = true; obs.disconnect(); nunca(timer); nunca(otra); };
  const mira = () => {
    if (hecho || timer) return;
    timer = later(() => {
      timer = 0;
      try {
        if (busca()) para();
        else if (oculto) { nunca(otra); otra = later(mira, 500); }
      } catch (e) { para(); }  // un fallo aquí no puede romper la página: se rinde
    }, 150);
  };
  const obs = new MO(mira);
  obs.observe(document, {childList: true, subtree: true, attributes: true,
                         attributeFilter: ['class', 'style', 'hidden', 'open']});
  addEventListener('load', () => later(para, 10000), {once: true});
  later(para, 30000);
  mira();
})();
