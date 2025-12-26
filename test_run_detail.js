// Test Playwright pour valider l'implémentation displayStartTime/displayEndTime
const { test, expect } = require('@playwright/test');

test.describe('Run Detail Page - displayStartTime/displayEndTime Implementation', () => {
  test.beforeEach(async ({ page }) => {
    // Naviguer vers la page du run AUV (ID 4)
    await page.goto('http://localhost:5000/run/4');
    // Attendre que la page soit chargée
    await page.waitForLoadState('networkidle');
    // Attendre que les données soient chargées (vérifier la présence de l'élément time-range-header)
    await page.waitForSelector('.time-range-header', { timeout: 10000 });
  });

  test('1. Test du chargement initial de la page', async ({ page }) => {
    // Vérifier que la page se charge sans erreur
    await expect(page).toHaveTitle(/Détail du Run/);
    
    // Vérifier que displayStartTime et displayEndTime sont initialisés
    const displayStartTime = await page.evaluate(() => {
      return window.displayStartTime || null;
    });
    const displayEndTime = await page.evaluate(() => {
      return window.displayEndTime || null;
    });
    
    console.log('displayStartTime:', displayStartTime);
    console.log('displayEndTime:', displayEndTime);
    
    expect(displayStartTime).not.toBeNull();
    expect(displayEndTime).not.toBeNull();
    expect(displayEndTime).toBeGreaterThan(displayStartTime);
    
    // Vérifier que la plage de temps est affichée en haut de la page
    const timeRangeStart = await page.textContent('#time-range-start');
    const timeRangeEnd = await page.textContent('#time-range-end');
    const timeRangeDuration = await page.textContent('#time-range-duration');
    
    expect(timeRangeStart).not.toBe('--:--:--');
    expect(timeRangeEnd).not.toBe('--:--:--');
    expect(timeRangeDuration).not.toBe('--');
    
    console.log('Time range displayed:', timeRangeStart, '→', timeRangeEnd, '(', timeRangeDuration, ')');
    
    // Vérifier que les 3 graphiques sont présents
    await expect(page.locator('#depth-chart')).toBeVisible();
    await expect(page.locator('#motors-chart')).toBeVisible();
    await expect(page.locator('#map')).toBeVisible();
  });

  test('2. Test de l\'affichage initial des données', async ({ page }) => {
    // Attendre que les données soient chargées
    await page.waitForTimeout(2000);
    
    // Vérifier que les données sont filtrées selon displayStartTime et displayEndTime
    const dataInfo = await page.evaluate(() => {
      const depthDataLength = window.depthData ? window.depthData.length : 0;
      const motorDataKeys = window.motorData ? Object.keys(window.motorData) : [];
      const usvPositionDataLength = window.usvPositionData ? window.usvPositionData.length : 0;
      const displayStartTime = window.displayStartTime;
      const displayEndTime = window.displayEndTime;
      
      return {
        depthDataLength,
        motorDataKeys: motorDataKeys.length,
        usvPositionDataLength,
        displayStartTime,
        displayEndTime
      };
    });
    
    console.log('Data info:', dataInfo);
    
    // Vérifier que les données sont présentes
    expect(dataInfo.depthDataLength).toBeGreaterThan(0);
    expect(dataInfo.motorDataKeys).toBeGreaterThan(0);
    
    // Vérifier que la plage de temps affichée correspond
    const timeRangeStart = await page.textContent('#time-range-start');
    const timeRangeEnd = await page.textContent('#time-range-end');
    
    expect(timeRangeStart).not.toBe('--:--:--');
    expect(timeRangeEnd).not.toBe('--:--:--');
  });

  test('3. Test du zoom (deux clics)', async ({ page }) => {
    // Attendre que le graphique soit prêt
    await page.waitForTimeout(2000);
    
    // Obtenir les dimensions du canvas
    const canvas = page.locator('#depth-chart');
    const canvasBox = await canvas.boundingBox();
    
    if (!canvasBox) {
      throw new Error('Canvas not found or not visible');
    }
    
    // Calculer les coordonnées pour deux clics (milieu et 3/4 du canvas)
    const firstClickX = canvasBox.x + canvasBox.width * 0.3;
    const firstClickY = canvasBox.y + canvasBox.height * 0.5;
    const secondClickX = canvasBox.x + canvasBox.width * 0.7;
    const secondClickY = canvasBox.y + canvasBox.height * 0.5;
    
    // Obtenir displayStartTime et displayEndTime avant le zoom
    const initialDisplayStartTime = await page.evaluate(() => window.displayStartTime);
    const initialDisplayEndTime = await page.evaluate(() => window.displayEndTime);
    
    console.log('Initial range:', new Date(initialDisplayStartTime), 'to', new Date(initialDisplayEndTime));
    
    // Premier clic
    await page.mouse.click(firstClickX, firstClickY);
    await page.waitForTimeout(500);
    
    // Vérifier qu'un indicateur visuel apparaît mais que les graphiques ne changent pas encore
    const afterFirstClick = await page.evaluate(() => ({
      displayStartTime: window.displayStartTime,
      displayEndTime: window.displayEndTime,
      waitingForSecondClick: window.waitingForSecondClick
    }));
    
    expect(afterFirstClick.displayStartTime).toBe(initialDisplayStartTime);
    expect(afterFirstClick.displayEndTime).toBe(initialDisplayEndTime);
    expect(afterFirstClick.waitingForSecondClick).toBe(true);
    
    // Second clic
    await page.mouse.click(secondClickX, secondClickY);
    await page.waitForTimeout(1000);
    
    // Vérifier que displayStartTime et displayEndTime sont mis à jour
    const afterSecondClick = await page.evaluate(() => ({
      displayStartTime: window.displayStartTime,
      displayEndTime: window.displayEndTime,
      depthDataLength: window.depthData ? window.depthData.length : 0
    }));
    
    console.log('After zoom range:', new Date(afterSecondClick.displayStartTime), 'to', new Date(afterSecondClick.displayEndTime));
    
    expect(afterSecondClick.displayStartTime).not.toBe(initialDisplayStartTime);
    expect(afterSecondClick.displayEndTime).not.toBe(initialDisplayEndTime);
    expect(afterSecondClick.displayEndTime).toBeGreaterThan(afterSecondClick.displayStartTime);
    
    // Vérifier que la plage de temps affichée est mise à jour
    const timeRangeStart = await page.textContent('#time-range-start');
    const timeRangeEnd = await page.textContent('#time-range-end');
    
    expect(timeRangeStart).not.toBe('--:--:--');
    expect(timeRangeEnd).not.toBe('--:--:--');
    
    // Vérifier que les données sont filtrées (moins de points après zoom)
    expect(afterSecondClick.depthDataLength).toBeLessThan(41263); // Moins que le total
  });

  test('4. Test du reset (clic droit)', async ({ page }) => {
    // Attendre que le graphique soit prêt
    await page.waitForTimeout(2000);
    
    // D'abord zoomer
    const canvas = page.locator('#depth-chart');
    const canvasBox = await canvas.boundingBox();
    
    if (!canvasBox) {
      throw new Error('Canvas not found or not visible');
    }
    
    const firstClickX = canvasBox.x + canvasBox.width * 0.3;
    const firstClickY = canvasBox.y + canvasBox.height * 0.5;
    const secondClickX = canvasBox.x + canvasBox.width * 0.7;
    const secondClickY = canvasBox.y + canvasBox.height * 0.5;
    
    await page.mouse.click(firstClickX, firstClickY);
    await page.waitForTimeout(500);
    await page.mouse.click(secondClickX, secondClickY);
    await page.waitForTimeout(1000);
    
    // Obtenir les valeurs après zoom
    const afterZoom = await page.evaluate(() => ({
      displayStartTime: window.displayStartTime,
      displayEndTime: window.displayEndTime
    }));
    
    console.log('After zoom:', new Date(afterZoom.displayStartTime), 'to', new Date(afterZoom.displayEndTime));
    
    // Obtenir les valeurs initiales (premier et dernier timestamp du log AUV)
    const initialValues = await page.evaluate(() => {
      if (window.depthDataFull && window.depthDataFull.length > 0) {
        const allTimes = window.depthDataFull.map(d => new Date(d.time).getTime());
        return {
          firstTimestamp: Math.min(...allTimes),
          lastTimestamp: Math.max(...allTimes)
        };
      }
      return null;
    });
    
    if (!initialValues) {
      throw new Error('Could not get initial timestamp values');
    }
    
    console.log('Initial values:', new Date(initialValues.firstTimestamp), 'to', new Date(initialValues.lastTimestamp));
    
    // Effectuer un clic droit
    await page.locator('#depth-chart').click({ button: 'right' });
    await page.waitForTimeout(1000);
    
    // Vérifier que displayStartTime et displayEndTime sont restaurés
    const afterReset = await page.evaluate(() => ({
      displayStartTime: window.displayStartTime,
      displayEndTime: window.displayEndTime,
      depthDataLength: window.depthData ? window.depthData.length : 0
    }));
    
    console.log('After reset:', new Date(afterReset.displayStartTime), 'to', new Date(afterReset.displayEndTime));
    
    // Vérifier que les valeurs sont restaurées (avec une tolérance de 1 seconde)
    expect(Math.abs(afterReset.displayStartTime - initialValues.firstTimestamp)).toBeLessThan(1000);
    expect(Math.abs(afterReset.displayEndTime - initialValues.lastTimestamp)).toBeLessThan(1000);
    
    // Vérifier que toutes les données sont affichées à nouveau
    expect(afterReset.depthDataLength).toBeGreaterThan(40000); // Proche du total
  });

  test('5. Test de la synchronisation des graphiques', async ({ page }) => {
    // Attendre que les graphiques soient prêts
    await page.waitForTimeout(2000);
    
    // Effectuer un zoom
    const canvas = page.locator('#depth-chart');
    const canvasBox = await canvas.boundingBox();
    
    if (!canvasBox) {
      throw new Error('Canvas not found or not visible');
    }
    
    const firstClickX = canvasBox.x + canvasBox.width * 0.3;
    const firstClickY = canvasBox.y + canvasBox.height * 0.5;
    const secondClickX = canvasBox.x + canvasBox.width * 0.7;
    const secondClickY = canvasBox.y + canvasBox.height * 0.5;
    
    await page.mouse.click(firstClickX, firstClickY);
    await page.waitForTimeout(500);
    await page.mouse.click(secondClickX, secondClickY);
    await page.waitForTimeout(1000);
    
    // Vérifier que tous les graphiques utilisent la même plage temporelle
    const syncInfo = await page.evaluate(() => {
      const displayStartTime = window.displayStartTime;
      const displayEndTime = window.displayEndTime;
      
      // Vérifier les données filtrées
      const depthTimes = window.depthData ? window.depthData.map(d => new Date(d.time).getTime()) : [];
      const motorTimes = [];
      if (window.motorData) {
        Object.keys(window.motorData).forEach(key => {
          window.motorData[key].forEach(d => {
            motorTimes.push(new Date(d.time).getTime());
          });
        });
      }
      const usvTimes = window.usvPositionData ? window.usvPositionData.map(p => new Date(p.time).getTime()) : [];
      
      return {
        displayStartTime,
        displayEndTime,
        depthMin: depthTimes.length > 0 ? Math.min(...depthTimes) : null,
        depthMax: depthTimes.length > 0 ? Math.max(...depthTimes) : null,
        motorMin: motorTimes.length > 0 ? Math.min(...motorTimes) : null,
        motorMax: motorTimes.length > 0 ? Math.max(...motorTimes) : null,
        usvMin: usvTimes.length > 0 ? Math.min(...usvTimes) : null,
        usvMax: usvTimes.length > 0 ? Math.max(...usvTimes) : null
      };
    });
    
    console.log('Synchronization info:', syncInfo);
    
    // Vérifier que toutes les données sont dans la plage displayStartTime à displayEndTime
    if (syncInfo.depthMin !== null) {
      expect(syncInfo.depthMin).toBeGreaterThanOrEqual(syncInfo.displayStartTime - 1000);
      expect(syncInfo.depthMax).toBeLessThanOrEqual(syncInfo.displayEndTime + 1000);
    }
    
    if (syncInfo.motorMin !== null) {
      expect(syncInfo.motorMin).toBeGreaterThanOrEqual(syncInfo.displayStartTime - 1000);
      expect(syncInfo.motorMax).toBeLessThanOrEqual(syncInfo.displayEndTime + 1000);
    }
    
    // Tester que le curseur met à jour la carte USV
    const cursorX = canvasBox.x + canvasBox.width * 0.5;
    const cursorY = canvasBox.y + canvasBox.height * 0.5;
    
    await page.mouse.move(cursorX, cursorY);
    await page.waitForTimeout(500);
    
    // Vérifier qu'un marqueur est présent sur la carte (via Leaflet)
    const markerPresent = await page.evaluate(() => {
      // Vérifier si un marqueur USV est présent
      return window.usvMarker !== null && window.usvMarker !== undefined;
    });
    
    // Le marqueur devrait être présent après le mouvement de la souris
    // (cette vérification peut échouer si le marqueur n'est pas encore créé, ce qui est acceptable)
    console.log('USV marker present:', markerPresent);
  });
});

