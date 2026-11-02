# Compositions des indices

Un fichier par indice (`index:` = identifiant de l'indice dans `instruments.yaml`).

* `ticker` : symbole **Yahoo** du titre (`MC.PA`, `SAP.DE`, `NOVO-B.CO`, `BRK-B`) ;
* `weight` : poids en % de l'indice ; `mic` : code de la place (XPAR, XETR, XMIL, XMAD,
  XAMS, XLON, XNYS, XNAS…) ; `currency` : devise de cotation ;
* `group: extended` (Stoxx 600) : chargé seulement à la demande ;
* `refresh` : d'où vient la composition (`ishares` + `url`, `fmp` + `list`, `manual`) ;
  `overrides: {TICKER_ISHARES: SYMBOLE_YAHOO}` corrige une correspondance.

Les listes Euro Stoxx 50, CAC 40, DAX, FTSE MIB et IBEX 35 sont des listes de départ
(fin 2025) à vérifier sur le site de l'indice ; `market-monitor constituents check` signale
les symboles qui ne répondent pas, `market-monitor constituents refresh` recalcule les poids.
