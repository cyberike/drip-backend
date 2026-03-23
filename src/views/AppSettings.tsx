import React from 'react';
import { ExtensionContextValue } from '@stripe/ui-extension-sdk/context';
import { fetchStripeSignature } from '@stripe/ui-extension-sdk/utils';
import {
  Box,
  Select,
  SettingsView,
  TextField,
} from '@stripe/ui-extension-sdk/ui';

const DRIP_API = 'https://dripfinancial.org';

type FormStatus = 'initial' | 'saving' | 'saved' | 'error';

interface Charity {
  id: number;
  name: string;
  ein: string;
  category: string;
}

const AppSettings = ({ userContext, environment }: ExtensionContextValue) => {
  const [status, setStatus] = React.useState<FormStatus>('initial');
  const [charities, setCharities] = React.useState<Charity[]>([]);
  const [currentSettings, setCurrentSettings] = React.useState<{
    donation_pct: string;
    charity_1: string;
    charity_2: string;
    charity_3: string;
  }>({
    donation_pct: '3',
    charity_1: '',
    charity_2: '',
    charity_3: '',
  });

  // Fetch available charities on mount
  React.useEffect(() => {
    const fetchCharities = async () => {
      try {
        const res = await fetch(`${DRIP_API}/api/charities?verified=true`);
        if (res.ok) {
          const data: Charity[] = await res.json();
          setCharities(data);
        }
      } catch (err) {
        console.error('Failed to load charities:', err);
      }
    };
    fetchCharities();
  }, []);

  // Fetch current settings on mount (authenticated)
  React.useEffect(() => {
    const acct = userContext?.account?.id;
    if (!acct) return;

    const fetchSettings = async () => {
      try {
        const headers: Record<string, string> = {
          'Stripe-Account': acct,
          'Stripe-User-Id': userContext?.id || '',
        };

        // Add signature for GET requests
        try {
          headers['Stripe-Signature'] = await fetchStripeSignature();
        } catch (sigErr) {
          console.warn('Could not fetch signature for GET:', sigErr);
        }

        const [settingsRes, allocRes] = await Promise.all([
          fetch(`${DRIP_API}/api/settings`, { headers }),
          fetch(`${DRIP_API}/api/allocations`, { headers }),
        ]);

        if (settingsRes.ok) {
          const settings = await settingsRes.json();
          setCurrentSettings((prev) => ({
            ...prev,
            donation_pct: String(settings.donation_pct || '3'),
          }));
        }

        if (allocRes.ok) {
          const allocs = await allocRes.json();
          const charityIds = allocs
            .sort((a: any, b: any) => b.pct_share - a.pct_share)
            .map((a: any) => String(a.charity_id));
          setCurrentSettings((prev) => ({
            ...prev,
            charity_1: charityIds[0] || '',
            charity_2: charityIds[1] || '',
            charity_3: charityIds[2] || '',
          }));
        }
      } catch (err) {
        console.error('Failed to load settings:', err);
      }
    };
    fetchSettings();
  }, [userContext, environment]);

  const saveSettings = React.useCallback(
    async (values: { [x: string]: string }) => {
      setStatus('saving');
      const acct = userContext?.account?.id;
      const userId = userContext?.id;

      try {
        // 1. Save donation percentage
        const pct = parseFloat(values.donation_pct);
        if (isNaN(pct) || pct < 1 || pct > 10) {
          setStatus('error');
          return;
        }

        // Build the settings payload with auth fields
        const settingsPayload = {
          donation_pct: pct,
          auto_donate: true,
          user_id: userId,
          account_id: acct,
        };

        // Get Stripe signature for the request
        const signature = await fetchStripeSignature();

        const headers: Record<string, string> = {
          'Content-Type': 'application/json',
          'Stripe-Signature': signature,
        };
        if (acct) headers['Stripe-Account'] = acct;

        await fetch(`${DRIP_API}/api/settings`, {
          method: 'PUT',
          headers,
          body: JSON.stringify(settingsPayload),
        });

        // 2. Build charity allocations from selected charities
        const selectedCharities = [
          values.charity_1,
          values.charity_2,
          values.charity_3,
        ].filter((id) => id && id !== '');

        if (selectedCharities.length > 0) {
          const uniqueCharities = [...new Set(selectedCharities)];
          const sharePerCharity = Math.round(
            (100 / uniqueCharities.length) * 100
          ) / 100;

          // Ensure shares sum to exactly 100
          const allocations = uniqueCharities.map((id, idx) => ({
            charity_id: parseInt(id, 10),
            pct_share:
              idx === uniqueCharities.length - 1
                ? 100 - sharePerCharity * (uniqueCharities.length - 1)
                : sharePerCharity,
          }));

          const allocPayload = {
            allocations,
            user_id: userId,
            account_id: acct,
          };

          // Get a fresh signature for this request
          const allocSignature = await fetchStripeSignature();

          await fetch(`${DRIP_API}/api/allocations`, {
            method: 'POST',
            headers: {
              'Content-Type': 'application/json',
              'Stripe-Signature': allocSignature,
              ...(acct ? { 'Stripe-Account': acct } : {}),
            },
            body: JSON.stringify(allocPayload),
          });
        }

        setStatus('saved');
      } catch (err) {
        console.error('Save failed:', err);
        setStatus('error');
      }
    },
    [userContext, environment]
  );

  const statusLabel = React.useMemo(() => {
    switch (status) {
      case 'saving':
        return 'Saving...';
      case 'saved':
        return 'Settings saved!';
      case 'error':
        return 'Error saving settings. Please check your values.';
      default:
        return '';
    }
  }, [status]);

  return (
    <SettingsView onSave={saveSettings} statusMessage={statusLabel}>
      <Box css={{ padding: 'large', stack: 'y', gap: 'large' }}>
        {/* Header */}
        <Box>
          <Box css={{ font: 'heading' }}>Drip Donations Settings</Box>
          <Box css={{ font: 'caption', color: 'secondary', marginTop: 'xsmall' }}>
            Configure what percentage of each payment goes to charity and
            choose up to 3 charities to support.
          </Box>
        </Box>

        {/* Donation Percentage */}
        <Box
          css={{
            padding: 'medium',
            backgroundColor: 'container',
            borderRadius: 'medium',
            stack: 'y',
            gap: 'small',
          }}
        >
          <Box css={{ font: 'subheading' }}>Donation Rate</Box>
          <TextField
            name="donation_pct"
            type="number"
            label="Percentage of each payment to donate (1-10%)"
            defaultValue={currentSettings.donation_pct}
            placeholder="3"
            size="medium"
          />
          <Box css={{ font: 'caption', color: 'secondary' }}>
            Example: At 3%, a $100 payment automatically donates $3.00
          </Box>
        </Box>

        {/* Charity Selection */}
        <Box
          css={{
            padding: 'medium',
            backgroundColor: 'container',
            borderRadius: 'medium',
            stack: 'y',
            gap: 'medium',
          }}
        >
          <Box css={{ font: 'subheading' }}>Choose Your Charities</Box>
          <Box css={{ font: 'caption', color: 'secondary' }}>
            Select up to 3 verified 501(c)(3) charities. Donations split
            equally among your selections.
          </Box>

          <Select
            name="charity_1"
            label="Charity 1 (Required)"
            defaultValue={currentSettings.charity_1}
          >
            <option value="">-- Select a charity --</option>
            {charities.map((c) => (
              <option key={c.id} value={String(c.id)}>
                {c.name} ({c.category})
              </option>
            ))}
          </Select>

          <Select
            name="charity_2"
            label="Charity 2 (Optional)"
            defaultValue={currentSettings.charity_2}
          >
            <option value="">-- None --</option>
            {charities.map((c) => (
              <option key={c.id} value={String(c.id)}>
                {c.name} ({c.category})
              </option>
            ))}
          </Select>

          <Select
            name="charity_3"
            label="Charity 3 (Optional)"
            defaultValue={currentSettings.charity_3}
          >
            <option value="">-- None --</option>
            {charities.map((c) => (
              <option key={c.id} value={String(c.id)}>
                {c.name} ({c.category})
              </option>
            ))}
          </Select>
        </Box>

        {/* Info */}
        <Box
          css={{
            padding: 'medium',
            backgroundColor: 'container',
            borderRadius: 'medium',
          }}
        >
          <Box css={{ font: 'caption', color: 'secondary' }}>
            Once saved, Drip will automatically calculate and track
            donations on every successful payment. View your donation
            history and generate year-end tax reports from the Drip
            dashboard.
          </Box>
        </Box>
      </Box>
    </SettingsView>
  );
};

export default AppSettings;
