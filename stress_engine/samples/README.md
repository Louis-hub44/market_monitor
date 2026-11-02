# Données d'exemple (fictives)

- `test_credit_eur.csv` : portefeuille de test crédit EUR, généré par `scripts/generate_test_portfolio.py`.
  Les identifiants `TEST-…` sont inventés. Les `SMPL-…` sont les titres synthétiques de l'univers d'exemple
  de Rates & Bond Analytics. Les prix des titres inventés sont calculés sur la courbe Bund d'exemple
  du 06/10/2026, plus un Z-spread par notation : ce sont des niveaux plausibles, **pas des cotations**.
- `test_lookthrough.csv` : compositions en transparence (`fund`, `weight` + gabarit de ligne) du fonds
  monétaire `MMF-B` et du fonds crédit `CREDIT-LT`.
