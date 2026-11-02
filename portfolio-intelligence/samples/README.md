# Portefeuilles de démonstration

Ces trois fichiers servent à **tester l'application sans données confidentielles**.

- Les **positions et les pondérations sont fictives** : elles ne correspondent à aucun
  portefeuille réel et n'ont aucune valeur d'allocation recommandée.
- Les **prix et spreads du fichier obligataire sont des valeurs d'exemple**, présentes
  uniquement pour que les calculs de duration s'exécutent. Ce ne sont pas des cotations
  de marché. Remplacez-les par vos données de valorisation avant toute utilisation.
- Les **caractéristiques contractuelles** (coupon, maturité, devise) sont, elles, des
  ordres de grandeur réalistes de titres souverains et corporate, mais doivent également
  être vérifiées avant usage.
- Les **tickers sont réels** : c'est ce qui permet aux modules Equity et Événements
  d'interroger les providers sur des sociétés existantes.

Format attendu : voir `services/portfolio/loader.py` (les en-têtes sont reconnus en
français comme en anglais, dans n'importe quel ordre).
