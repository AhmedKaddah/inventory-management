<template>
  <div class="restocking">
    <div class="page-header">
      <h2>{{ t('restocking.title') }}</h2>
      <p>{{ t('restocking.description') }}</p>
    </div>

    <div class="card">
      <div class="card-header">
        <h3 class="card-title">{{ t('restocking.budgetTitle') }}</h3>
        <span class="budget-value">{{ formatMoney(budget) }}</span>
      </div>
      <div class="budget-control">
        <input
          type="range"
          class="budget-slider"
          min="0"
          :max="sliderMax"
          step="100"
          :value="budget"
          :aria-label="t('restocking.budgetLabel')"
          @input="onBudgetInput"
        />
        <div class="budget-scale">
          <span>{{ formatMoney(0) }}</span>
          <span>{{ formatMoney(sliderMax) }}</span>
        </div>
      </div>
    </div>

    <div v-if="placedOrder" class="success-banner">
      <strong>{{ t('restocking.success.title') }}</strong>
      <span>{{ t('restocking.success.orderNumber') }}: {{ placedOrder.order_number }}</span>
      <span>{{ t('restocking.success.expectedDelivery') }}: {{ formatDate(placedOrder.expected_delivery_date) }}</span>
      <span>{{ t('restocking.success.leadTime') }}: {{ t('restocking.success.days', { count: placedOrder.lead_time_days }) }}</span>
    </div>

    <div v-if="loading && !recommendations" class="loading">{{ t('common.loading') }}</div>
    <div v-else-if="loadError" class="error">{{ loadError }}</div>
    <div v-else-if="recommendations">
      <div class="stats-grid">
        <div class="stat-card">
          <div class="stat-label">{{ t('restocking.budget') }}</div>
          <div class="stat-value">{{ formatMoney(recommendations.budget) }}</div>
        </div>
        <div class="stat-card info">
          <div class="stat-label">{{ t('restocking.spent') }}</div>
          <div class="stat-value">{{ formatMoney(recommendations.spent) }}</div>
        </div>
        <div class="stat-card success">
          <div class="stat-label">{{ t('restocking.remaining') }}</div>
          <div class="stat-value">{{ formatMoney(recommendations.remaining) }}</div>
        </div>
        <div class="stat-card warning">
          <div class="stat-label">{{ t('restocking.itemsToRestock') }}</div>
          <div class="stat-value">{{ items.length }}</div>
        </div>
      </div>

      <div class="card">
        <div class="card-header">
          <h3 class="card-title">{{ t('restocking.recommendations') }} ({{ items.length }})</h3>
          <button
            type="button"
            class="place-order-btn"
            :disabled="items.length === 0 || submitting || loading || budgetPending"
            @click="placeOrder"
          >
            {{ submitting ? t('restocking.submitting') : t('restocking.placeOrder') }}
          </button>
        </div>

        <div v-if="submitError" class="error">{{ submitError }}</div>

        <div v-if="items.length === 0" class="loading">{{ t('restocking.empty') }}</div>
        <div v-else class="table-container">
          <table>
            <thead>
              <tr>
                <th>{{ t('restocking.table.sku') }}</th>
                <th>{{ t('restocking.table.item') }}</th>
                <th>{{ t('restocking.table.supplier') }}</th>
                <th>{{ t('restocking.table.onHand') }}</th>
                <th>{{ t('restocking.table.forecastedDemand') }}</th>
                <th>{{ t('restocking.table.trend') }}</th>
                <th>{{ t('restocking.table.quantity') }}</th>
                <th>{{ t('restocking.table.unitCost') }}</th>
                <th>{{ t('restocking.table.lineTotal') }}</th>
                <th>{{ t('restocking.table.leadTime') }}</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="item in items" :key="item.sku">
                <td><strong>{{ item.sku }}</strong></td>
                <td>{{ item.item_name }}</td>
                <td>{{ item.supplier_name }}</td>
                <td>
                  {{ item.on_hand }}
                  <span v-if="!item.has_stock_record" class="badge warning">{{ t('restocking.noStockRecord') }}</span>
                </td>
                <td>{{ item.forecasted_demand }}</td>
                <td>
                  <span :class="['badge', item.trend]">{{ t(`trends.${item.trend}`) }}</span>
                </td>
                <td><strong>{{ item.recommended_quantity }}</strong></td>
                <td>{{ formatUnitCost(item.unit_cost) }}</td>
                <td><strong>{{ formatMoney(item.line_total) }}</strong></td>
                <td>{{ item.lead_time_days }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>
    </div>
  </div>
</template>

<script>
import { ref, computed, onMounted, onBeforeUnmount } from 'vue'
import { api } from '../api'
import { useI18n } from '../composables/useI18n'
import { formatCurrency, formatCurrencyWithDecimals } from '../utils/currency'

export default {
  name: 'Restocking',
  setup() {
    const { t, currentCurrency, currentLocale } = useI18n()

    // Budget is always held in USD; formatCurrency converts for display
    const budget = ref(10000)
    const recommendations = ref(null)
    const loading = ref(true)
    const loadError = ref(null)

    // True from slider input until the matching reload finishes; guards against ordering a stale list
    const budgetPending = ref(false)
    const submitting = ref(false)
    const submitError = ref(null)
    const placedOrder = ref(null)

    const items = computed(() => recommendations.value?.items || [])
    const maxBudget = computed(() => recommendations.value?.max_budget ?? 10000)
    // The slider step is 100, and browsers snap max down to a step multiple, so round up to keep max_budget reachable
    const sliderMax = computed(() => Math.ceil(maxBudget.value / 100) * 100)

    const formatMoney = (amount) => formatCurrency(Number(amount) || 0, currentCurrency.value)

    // Unit costs are fractional (e.g. $3.20), so show 2 decimals; JPY is rounded to whole yen by the util
    const formatUnitCost = (amount) => formatCurrencyWithDecimals(Number(amount) || 0, currentCurrency.value, 2)

    const formatDate = (dateString) => {
      // Date-only strings parse as UTC midnight, which shows the previous day west of UTC; build a local date instead
      const dateOnly = typeof dateString === 'string' && dateString.match(/^(\d{4})-(\d{2})-(\d{2})$/)
      const date = dateOnly
        ? new Date(Number(dateOnly[1]), Number(dateOnly[2]) - 1, Number(dateOnly[3]))
        : new Date(dateString)
      if (!dateString || isNaN(date.getTime())) return '-'
      const locale = currentLocale.value === 'ja' ? 'ja-JP' : 'en-US'
      return date.toLocaleDateString(locale, { year: 'numeric', month: 'short', day: 'numeric' })
    }

    // Counter so a slow earlier response cannot overwrite a newer one
    let latestRequest = 0
    const loadRecommendations = async () => {
      const id = ++latestRequest
      try {
        loading.value = true
        loadError.value = null
        const data = await api.getRestockingRecommendations(budget.value)
        if (id !== latestRequest) return
        recommendations.value = data
        // max_budget is only known after a fetch, so clamp a budget that exceeds the rounded slider max
        const limit = Math.ceil(data.max_budget / 100) * 100
        if (budget.value > limit) {
          budget.value = limit
          return loadRecommendations()
        }
      } catch (err) {
        if (id !== latestRequest) return
        loadError.value = t('restocking.loadFailed')
        console.error('Recommendations load error:', err)
      } finally {
        if (id === latestRequest) {
          loading.value = false
          budgetPending.value = false
        }
      }
    }

    // Debounce so dragging the slider does not fire a request per step
    let debounceTimer = null
    const onBudgetInput = (event) => {
      budget.value = Math.min(Number(event.target.value), sliderMax.value)
      budgetPending.value = true
      clearTimeout(debounceTimer)
      debounceTimer = setTimeout(loadRecommendations, 300)
    }

    const placeOrder = async () => {
      if (items.value.length === 0 || submitting.value) return
      submitting.value = true
      submitError.value = null
      placedOrder.value = null
      try {
        placedOrder.value = await api.placeRestockingOrder({
          // Use the budget the table was built from, not the live slider value
          budget: recommendations.value.budget,
          lines: items.value.map(i => ({ sku: i.sku, quantity: i.recommended_quantity }))
        })
        // Re-fetch: the server subtracts open restock quantities from need
        await loadRecommendations()
      } catch (err) {
        submitError.value = err.response?.data?.detail || t('restocking.submitFailed')
        console.error('Place order error:', err)
      } finally {
        submitting.value = false
      }
    }

    onMounted(loadRecommendations)
    onBeforeUnmount(() => clearTimeout(debounceTimer))

    return {
      t,
      budget,
      maxBudget,
      sliderMax,
      budgetPending,
      recommendations,
      items,
      loading,
      loadError,
      submitting,
      submitError,
      placedOrder,
      formatMoney,
      formatUnitCost,
      formatDate,
      onBudgetInput,
      placeOrder
    }
  }
}
</script>

<style scoped>
.budget-value {
  font-size: 1.5rem;
  font-weight: 700;
  color: #0f172a;
}

.budget-control {
  padding: 0.5rem 1.5rem 1.5rem;
}

.budget-slider {
  width: 100%;
  accent-color: #475569;
}

.budget-scale {
  display: flex;
  justify-content: space-between;
  font-size: 0.813rem;
  color: #64748b;
}

.place-order-btn {
  background: #0f172a;
  color: white;
  border: none;
  border-radius: 8px;
  padding: 0.5rem 1.25rem;
  font-size: 0.875rem;
  font-weight: 600;
  cursor: pointer;
  transition: background 0.2s;
}

.place-order-btn:hover:not(:disabled) {
  background: #1e293b;
}

.place-order-btn:disabled {
  background: #cbd5e1;
  cursor: not-allowed;
}

.success-banner {
  display: flex;
  flex-wrap: wrap;
  gap: 0.5rem 1.5rem;
  background: #f0fdf4;
  border: 1px solid #bbf7d0;
  color: #166534;
  padding: 1rem;
  border-radius: 8px;
  margin: 1rem 0;
  font-size: 0.938rem;
}

.badge {
  margin-left: 0.5rem;
}
</style>
