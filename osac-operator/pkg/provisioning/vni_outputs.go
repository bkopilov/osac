package provisioning

import (
	"encoding/json"
	"fmt"
	"math"
	"strconv"
)

const maxVNI = 1<<24 - 1

type FabricVNIs struct {
	L2VNI *int32
	L3VNI *int32
}

func ParseFabricVNIs(extraVars map[string]any) (FabricVNIs, error) {
	l2VNI, err := parseVNI(extraVars, "l2_vni")
	if err != nil {
		return FabricVNIs{}, err
	}
	l3VNI, err := parseVNI(extraVars, "l3_vni")
	if err != nil {
		return FabricVNIs{}, err
	}

	return FabricVNIs{L2VNI: l2VNI, L3VNI: l3VNI}, nil
}

func parseVNI(extraVars map[string]any, key string) (*int32, error) {
	value, found := extraVars[key]
	if !found || value == nil {
		return nil, nil
	}

	var parsed int64
	switch value := value.(type) {
	case float64:
		if math.IsNaN(value) || math.IsInf(value, 0) || math.Trunc(value) != value {
			return nil, invalidVNI(key, value)
		}
		parsed = int64(value)
	case float32:
		if math.IsNaN(float64(value)) || math.IsInf(float64(value), 0) || math.Trunc(float64(value)) != float64(value) {
			return nil, invalidVNI(key, value)
		}
		parsed = int64(value)
	case json.Number:
		var err error
		parsed, err = value.Int64()
		if err != nil {
			return nil, invalidVNI(key, value)
		}
	case string:
		var err error
		parsed, err = strconv.ParseInt(value, 10, 64)
		if err != nil {
			return nil, invalidVNI(key, value)
		}
	case int:
		parsed = int64(value)
	case int32:
		parsed = int64(value)
	case int64:
		parsed = value
	case uint:
		if uint64(value) > math.MaxInt64 {
			return nil, invalidVNI(key, value)
		}
		parsed = int64(value)
	case uint32:
		parsed = int64(value)
	case uint64:
		if value > math.MaxInt64 {
			return nil, invalidVNI(key, value)
		}
		parsed = int64(value)
	default:
		return nil, invalidVNI(key, value)
	}

	if parsed < 1 || parsed > maxVNI {
		return nil, invalidVNI(key, value)
	}
	vni := int32(parsed)
	return &vni, nil
}

func invalidVNI(key string, value any) error {
	return fmt.Errorf("%s must be an integer between 1 and %d, got %v (%T)", key, maxVNI, value, value)
}
