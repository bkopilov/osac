package provisioning

import (
	"encoding/json"
	"math"

	"github.com/onsi/ginkgo/v2"
	. "github.com/onsi/gomega"
)

var _ = ginkgo.Describe("ParseFabricVNIs", func() {
	ginkgo.It("parses integer values returned from AAP job artifacts", func() {
		vnis, err := ParseFabricVNIs(map[string]any{"l2_vni": float64(4096), "l3_vni": json.Number("16777215")})

		Expect(err).NotTo(HaveOccurred())
		Expect(*vnis.L2VNI).To(Equal(int32(4096)))
		Expect(*vnis.L3VNI).To(Equal(int32(16777215)))
	})

	ginkgo.It("accepts a missing optional VNI", func() {
		vnis, err := ParseFabricVNIs(map[string]any{"l2_vni": float64(4096)})

		Expect(err).NotTo(HaveOccurred())
		Expect(vnis.L2VNI).NotTo(BeNil())
		Expect(vnis.L3VNI).To(BeNil())
	})

	ginkgo.DescribeTable("rejects invalid supplied values",
		func(value any) {
			_, err := ParseFabricVNIs(map[string]any{"l2_vni": value})
			Expect(err).To(HaveOccurred())
		},
		ginkgo.Entry("zero", float64(0)),
		ginkgo.Entry("negative", float64(-1)),
		ginkgo.Entry("fractional", float64(1.5)),
		ginkgo.Entry("above the 24-bit maximum", float64(1<<24)),
		ginkgo.Entry("malformed string", "not-a-vni"),
		ginkgo.Entry("non-finite number", math.NaN()),
	)
})
